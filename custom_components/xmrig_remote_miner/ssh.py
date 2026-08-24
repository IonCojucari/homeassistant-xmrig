"""Minimal SSH client for host power control.

Kept apart from the rest of the integration on purpose: everything touching SSH
is optional and must be able to fail without consequence. If `asyncssh` cannot
be installed, if the key is missing, or if the host has never heard of
`rig-power`, the integration goes on monitoring the miner exactly as before,
simply with fewer entities.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass

from .const import (
    ACTION_OFF,
    ACTION_REBOOT,
    ACTION_SUSPEND,
    CAPS_ACTIONS,
    CAPS_COMMAND,
    CAPS_MAC,
    MAC_PROBE,
    MAC_UNRESOLVED,
    RIG_POWER_PLAIN,
    RIG_POWER_PROBE,
    RIG_POWER_PROBE_PLAIN,
    RIG_POWER_SUDO,
    WOL_PORT,
)

_LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - depends on the runtime environment
    import asyncssh
except ImportError:
    asyncssh = None

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)

# Raised when the remote end goes away in the middle of a command -- which is
# the *expected* outcome of `rig-power off`, not a failure.
#
# asyncssh.ConnectionLost has to be named explicitly: it derives from
# asyncssh.Error, which derives straight from Exception, so it is neither an
# OSError nor a ConnectionError and a builtin-only tuple never catches it. The
# visible symptom of getting this wrong is Home Assistant showing "shutdown
# failed" every single time the machine shut down correctly.
#
# Deliberately not asyncssh.Error at large: PermissionDenied is also an
# asyncssh.Error, and a key that stopped working must stay loud rather than be
# reported as a successful shutdown.
DISCONNECTED_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    EOFError,
) + ((asyncssh.ConnectionLost,) if asyncssh is not None else ())

# No separator marker, and no single chained command. The two halves of the
# probe used to go out as one `A 2>/dev/null; echo SEP; B` to save a round trip
# -- except the cost is the *connection*, not the command, and asyncssh can open
# several sessions on one connection. Chaining therefore bought nothing and
# imposed POSIX syntax, which made the probe unusable against OpenSSH on
# Windows, where it opens cmd.exe.


@dataclass(frozen=True)
class PowerCapabilities:
    """What this machine actually accepts, as it declared itself.

    `command` keeps the invocation form that answered the probe, so the action
    uses exactly that one. Discovering it and then guessing it again at shutdown
    time would be an opportunity to get the call that matters most wrong.
    """

    actions: frozenset[str]
    mac: str | None
    command: str = RIG_POWER_SUDO

    @property
    def can_power_off(self) -> bool:
        return ACTION_OFF in self.actions

    @property
    def can_reboot(self) -> bool:
        return ACTION_REBOOT in self.actions

    @property
    def can_suspend(self) -> bool:
        return ACTION_SUSPEND in self.actions

    @property
    def can_wake(self) -> bool:
        return self.mac is not None

    @property
    def any(self) -> bool:
        return (
            self.can_power_off
            or self.can_reboot
            or self.can_suspend
            or self.can_wake
        )

    def as_dict(self) -> dict:
        """Serialisable form, for storing in the config entry."""
        return {
            CAPS_ACTIONS: sorted(self.actions),
            CAPS_MAC: self.mac,
            CAPS_COMMAND: self.command,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PowerCapabilities | None:
        """Read back what `as_dict` wrote. None if nothing usable.

        Tolerant by construction: this data was written by an earlier version of
        the integration and is not revalidated anywhere else. A missing field, or
        one of an unexpected type, must yield "no known capabilities" -- which
        simply triggers a fresh probe -- and not an exception that would stop the
        entry from loading.
        """
        if not isinstance(data, dict):
            return None
        actions = data.get(CAPS_ACTIONS)
        if not isinstance(actions, (list, tuple, set, frozenset)):
            actions = ()
        mac = data.get(CAPS_MAC)
        # `or RIG_POWER_SUDO` would be wrong here: HassAgentPower stores an
        # empty command on purpose, meaning "this host is driven through
        # HASS.Agent, not through SSH". Collapsing that to the sudo form makes a
        # Windows rig try `sudo -n rig-power off` over SSH after a restart.
        # Only a missing or non-string value falls back.
        command = data.get(CAPS_COMMAND)
        if not isinstance(command, str):
            command = RIG_POWER_SUDO

        caps = cls(
            actions=frozenset(
                a
                for a in actions
                if a in (ACTION_OFF, ACTION_REBOOT, ACTION_SUSPEND)
            ),
            mac=mac if isinstance(mac, str) and _MAC_RE.match(mac) else None,
            command=command,
        )
        return caps if caps.any else None


class SshRunner:
    """Runs short commands on the rig, one session per command."""

    def __init__(self, host: str, port: int, user: str, key_path: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path

    @property
    def available(self) -> bool:
        """Whether SSH can be attempted at all.

        An empty user is an explicit opt-out, and the config flow labels the
        field that way. It has to be checked here rather than left to the
        default, because `entry.data.get(key, DEFAULT)` returns "" for a key
        that is present and empty -- so clearing the field used to produce a
        real connection and a failed authentication on every setup instead of
        skipping SSH.
        """
        return asyncssh is not None and bool(self.user)

    async def async_run(self, command: str, timeout: int = 20) -> str:
        """One command, one connection. Used for the actions."""
        results = await self.async_run_many([command], timeout=timeout)
        return results[0]

    async def async_run_many(
        self, commands: list[str], timeout: int = 20
    ) -> list[str]:
        """Several commands over a single connection, in order.

        Opening the connection is what costs -- handshake, key exchange. The
        sessions that follow are nearly free. Going through them rather than
        through a `;` avoids imposing a POSIX shell on the remote machine.
        """
        if asyncssh is None:
            raise RuntimeError("asyncssh is not available")
        if not self.user:
            raise RuntimeError("no SSH user configured")

        async with asyncio.timeout(timeout):
            # known_hosts=None disables host key verification. A deliberate
            # trade-off: these are LAN machines, their host key changes on
            # every reinstall, and the only alternative would be asking the user
            # to maintain a known_hosts file by hand inside a container. The
            # residual risk is a LAN neighbour able to spoof the rig's address;
            # all they would gain is a `rig-power off`.
            async with asyncssh.connect(
                self.host,
                port=self.port,
                username=self.user,
                client_keys=[self.key_path],
                known_hosts=None,
            ) as conn:
                out = []
                for command in commands:
                    result = await conn.run(command, check=False)
                    out.append(str(result.stdout or ""))
                return out

    async def async_probe(self) -> PowerCapabilities | None:
        """Ask the machine what it permits. None = nothing to do.

        `rig-power status` exists precisely for this question: discovering the
        permitted actions without triggering one. Probing by attempting a
        shutdown would obviously be unacceptable.
        """
        if not self.available:
            _LOGGER.debug(
                "SSH unavailable for %s (asyncssh missing, or no user set):"
                " no host power control",
                self.host,
            )
            return None

        # Three commands, one connection. The sudo form first: it is what every
        # existing rig uses and it must stay the nominal path. The sudo-less
        # fallback is only for machines that have no sudo.
        try:
            sudo_out, plain_out, mac_out = await self.async_run_many(
                [RIG_POWER_PROBE, RIG_POWER_PROBE_PLAIN, MAC_PROBE]
            )
        except Exception as err:  # noqa: BLE001 - a probe must not break anything
            _LOGGER.debug("SSH probe against %s failed: %s", self.host, err)
            return None

        actions = _parse_actions(sudo_out)
        command = RIG_POWER_SUDO
        if not actions:
            actions = _parse_actions(plain_out)
            command = RIG_POWER_PLAIN

        mac = None
        for line in mac_out.splitlines():
            candidate = line.strip().lower()
            if _MAC_RE.match(candidate):
                mac = candidate
                break

        caps = PowerCapabilities(
            actions=frozenset(
                actions & {ACTION_OFF, ACTION_REBOOT, ACTION_SUSPEND}
            ),
            mac=mac,
            command=command,
        )
        if not caps.any:
            _LOGGER.debug("%s declares no power actions", self.host)
            return None

        _LOGGER.debug(
            "%s: actions=%s mac=%s via %s",
            self.host,
            sorted(caps.actions),
            caps.mac,
            caps.command,
        )
        return caps


def _parse_actions(output: str) -> set[str]:
    """Pull the `actions: off, reboot, status` line out of a probe's output."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("actions:"):
            return {a.strip() for a in line.split(":", 1)[1].split(",") if a.strip()}
    return set()


def resolve_mac_via_arp(host: str) -> str | None:
    """Resolve the MAC from Home Assistant, over ARP. Blocking.

    A fallback for when the machine could not give it itself -- a Windows host,
    where `ip` does not exist. Deliberately the second choice: the SSH probe
    reads the card holding the default route, which is authoritative, whereas
    ARP can only answer if Home Assistant shares the machine's layer-2 segment
    and the machine is switched on. That holds for a rig on the LAN; it does not
    necessarily hold for Home Assistant in a container on a bridge network.

    getmac sometimes returns an all-zero MAC rather than None when resolution
    fails, hence the explicit rejection.
    """
    try:
        from getmac import get_mac_address
    except ImportError:  # pragma: no cover - depends on the environment
        _LOGGER.debug("getmac missing: no fallback MAC resolution")
        return None

    try:
        mac = get_mac_address(ip=host)
    except Exception as err:  # noqa: BLE001 - a fallback must not break anything
        _LOGGER.debug("ARP resolution of %s failed: %s", host, err)
        return None

    if not mac:
        return None
    mac = mac.lower()
    if mac == MAC_UNRESOLVED or not _MAC_RE.match(mac):
        _LOGGER.debug("ARP resolution of %s gave no usable answer", host)
        return None
    return mac


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Wake the machine. Blocking, so run it in an executor.

    No dependency needed: a magic packet is six 0xFF bytes followed by sixteen
    repetitions of the MAC, sent as a UDP broadcast. Waking cannot go over SSH
    -- the machine is off, there is no server left to answer -- hence this
    second mechanism, unrelated to the first.
    """
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, WOL_PORT))
