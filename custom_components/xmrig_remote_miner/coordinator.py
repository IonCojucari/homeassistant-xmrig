"""Polling coordinator and HTTP client for one XMRig instance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_GLANCES_PASSWORD,
    CONF_GLANCES_PORT,
    CONF_GLANCES_USER,
    CONF_HASS_AGENT_DEVICE,
    CONF_POWER_CAPS,
    CONF_SSH_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USER,
    DEFAULT_GLANCES_PORT,
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    GLANCES_API_VERSIONS,
    GLANCES_PLUGINS,
    MANUFACTURER,
    RIG_POWER_SUDO,
)
from .hass_agent import HassAgentPower
from .hass_agent import async_probe as async_probe_hass_agent
from .ssh import (
    DISCONNECTED_ERRORS,
    PowerCapabilities,
    SshRunner,
    resolve_mac_via_arp,
)

_LOGGER = logging.getLogger(__name__)


# XMRig's user-agent string carries the OS in parentheses, first:
#   XMRig/6.24.0 (Linux x86_64) libuv/1.48.0 gcc/13.3.0
#   XMRig/6.24.0 (Windows NT 10.0; Win64; x64) libuv/1.48.0 msvc/2022
# Keep the first segment before ";", which is the part naming the OS; the rest
# describes the ABI and says nothing more.
_UA_OS = re.compile(r"\(([^)]*)\)")


def miner_os(data: dict) -> str | None:
    """The machine's OS, as XMRig describes itself.

    Taken from the user-agent string rather than from Glances' `system` plugin:
    it is already in the summary fetched on every poll, whereas Glances would
    cost one more HTTP request each time for a fact that never changes. Less
    elegant, free.
    """
    match = _UA_OS.search(data.get("ua") or "")
    if not match:
        return None
    return match.group(1).split(";")[0].strip() or None


def miner_memory_total(data: dict) -> int | None:
    """Total RAM, in bytes.

    Makes the memory-usage sensor interpretable: a percentage alone does not say
    whether 400 MB or 12 GB is left. Free memory gets no sensor of its own for
    that -- with the total and the percentage, it follows.
    """
    memory = (data.get("resources") or {}).get("memory") or {}
    total = memory.get("total")
    return int(total) if total else None


class XmrigCoordinator(DataUpdateCoordinator[dict]):
    """Polls the XMRig summary and sends the control commands.

    Also polls, if it answers, a Glances instance on the same machine for
    temperature and load -- numbers XMRig does not report and that explain half
    the hashrate variation. That second source is strictly optional: it cannot
    fail a poll, and its sensors are not even created if Glances does not answer
    at start-up.

    A dedicated aiohttp session with ``force_close=True`` is used: XMRig answers
    "Connection: close" on every request, which stales the connections a shared
    pool keeps and causes intermittent ClientErrors on the command POST. Forcing
    a fresh connection per call makes the problem disappear for both reads and
    writes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
    ) -> None:
        self.entry = entry
        self.session = session
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._token: str = entry.data[CONF_TOKEN]
        self.glances_port: int = entry.data.get(
            CONF_GLANCES_PORT, DEFAULT_GLANCES_PORT
        )
        # Glances HTTP Basic credentials, when it asks for them. Absent for an
        # unprotected instance, which is still Glances' default.
        glances_user = entry.data.get(CONF_GLANCES_USER)
        self._glances_auth: aiohttp.BasicAuth | None = (
            aiohttp.BasicAuth(
                glances_user, entry.data.get(CONF_GLANCES_PASSWORD) or ""
            )
            if glances_user
            else None
        )
        # The Glances API version settled on at first contact, or None while
        # none has been reached successfully yet.
        self._glances_api: int | None = None
        self._glances_auth_warned = False

        self.ssh = SshRunner(
            host=self.host,
            port=entry.data.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
            user=entry.data.get(CONF_SSH_USER, DEFAULT_SSH_USER),
            key_path=entry.data.get(CONF_SSH_KEY, DEFAULT_SSH_KEY),
        )
        # What the last successful probe established, read back from the config
        # entry. This is what lets the buttons exist even while the machine is
        # off -- the wake button is only wanted at that moment, and a probe can
        # learn nothing from a machine that is not running. None means "nothing
        # was ever known", not "there is nothing to do".
        self.power_capabilities: PowerCapabilities | None = (
            PowerCapabilities.from_dict(entry.data.get(CONF_POWER_CAPS))
        )
        # Explicitly chosen HASS.Agent device, or None for automatic matching
        # on the worker_id XMRig publishes.
        self.hass_agent_device: str | None = entry.data.get(CONF_HASS_AGENT_DEVICE)
        # Set only when HASS.Agent is what provides power control; None means
        # "SSH", including when there is nothing at all.
        self._hass_agent: HassAgentPower | None = None
        # Clamped, not trusted. The config flow now enforces a minimum, but an
        # entry created before it did could hold 0 -- and an update_interval of
        # zero means the next refresh is always overdue, i.e. a loop that polls
        # the miner's HTTP API as fast as the event loop allows.
        interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        if not isinstance(interval, int) or interval < MIN_SCAN_INTERVAL:
            interval = MIN_SCAN_INTERVAL
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly: relying on the implicit current-entry
            # ContextVar is deprecated for custom integrations and is due to be
            # removed.
            config_entry=entry,
            name=f"XMRig {entry.title}",
            update_interval=timedelta(seconds=interval),
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def miner(self) -> dict:
        """The XMRig summary from the last poll."""
        return (self.data or {}).get("miner") or {}

    @property
    def worker_id(self) -> str | None:
        """The name the miner gives itself. Used to find the HASS.Agent device."""
        return self.miner.get("worker_id") or None

    @property
    def glances(self) -> dict | None:
        """Glances telemetry, or None if unavailable/disabled."""
        return (self.data or {}).get("glances")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _async_update_data(self) -> dict:
        """Poll the miner, then the system telemetry if it is there.

        Only XMRig can fail the poll. Glances is a bonus: a telemetry outage
        must not take the hashrate and the shares down with it, or the
        measurement is lost at exactly the moment it is needed.
        """
        miner = await self._async_fetch_miner()
        return {"miner": miner, "glances": await self._async_fetch_glances()}

    async def _async_fetch_miner(self) -> dict:
        try:
            async with asyncio.timeout(10):
                async with self.session.get(
                    f"{self.base_url}/1/summary", headers=self._headers
                ) as resp:
                    if resp.status in (401, 403):
                        raise UpdateFailed("access token refused (401/403)")
                    resp.raise_for_status()
                    return await resp.json()
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise UpdateFailed("timed out polling XMRig") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"connection error talking to XMRig: {err}") from err

    async def _async_fetch_glances(self) -> dict | None:
        """Return {plugin: payload}, or None if Glances does not answer."""
        if not self.glances_port:
            return None

        versions = (
            (self._glances_api,) if self._glances_api else GLANCES_API_VERSIONS
        )
        for version in versions:
            base = f"http://{self.host}:{self.glances_port}/api/{version}"
            try:
                async with asyncio.timeout(10):
                    # return_exceptions, so that one plugin failing does not
                    # abandon the other three still in flight: gather would
                    # propagate immediately and leave them running outside this
                    # timeout with nobody collecting their results, which shows
                    # up as "Task exception was never retrieved". The normal way
                    # to hit that is a Glances 3 host being asked for /api/4.
                    results = await asyncio.gather(
                        *(self._async_get_json(f"{base}/{p}") for p in GLANCES_PLUGINS),
                        return_exceptions=True,
                    )
            except (TimeoutError, asyncio.TimeoutError):
                continue

            # One failure disqualifies this API version, with one exception
            # worth telling apart: a 401/403 is not "Glances is absent" but
            # "Glances is there and refuses these credentials". Without that
            # distinction, a protected instance with the wrong password looks
            # exactly like a machine with no Glances -- four sensors that never
            # exist, and nothing to say why.
            failure = next(
                (r for r in results if isinstance(r, BaseException)), None
            )
            if failure is not None:
                if (
                    isinstance(failure, aiohttp.ClientResponseError)
                    and failure.status in (401, 403)
                ):
                    # Warn once. The poll runs every 20 seconds and a wrong
                    # password stays wrong until someone fixes it; repeating
                    # would teach nothing and drown the log.
                    if not self._glances_auth_warned:
                        self._glances_auth_warned = True
                        _LOGGER.warning(
                            "Glances on %s:%s refuses the credentials (%s) —"
                            " system sensors skipped. Fix them with"
                            " Reconfigure on this rig.",
                            self.host,
                            self.glances_port,
                            failure.status,
                        )
                    return None
                continue

            self._glances_api = version
            return dict(zip(GLANCES_PLUGINS, results, strict=True))

        # Unlatch the remembered version. Without this, a rig upgraded from
        # Glances 3 to 4 would keep being asked for /api/3 forever: the loop
        # above narrows to the remembered version once one has worked, so a
        # permanent 404 there would take the system sensors down until the
        # entry was reloaded.
        self._glances_api = None
        _LOGGER.debug(
            "Glances unreachable on %s:%s, system sensors skipped",
            self.host,
            self.glances_port,
        )
        return None

    async def async_probe_power(self) -> None:
        """Ask the machine once what it permits.

        Two sources, in this strict order:

        1. SSH and `rig-power status`, the nominal path. A NixOS rig answers
           here and never sees the rest.
        2. HASS.Agent, for machines with no way to answer the first -- a Windows
           host, in practice.

        Then, only if a source has already established capabilities but without
        a MAC, an ARP fallback for waking. That condition is not cosmetic:
        applying the fallback to a machine whose probe returned nothing would
        make a wake button appear on rigs that had none, merely because they
        answer a ping. The fallback can therefore only complete, never create.

        An unsuccessful probe never erases what was already known: a machine
        that is off answers neither SSH nor ARP, and taking that silence for
        "this machine has no wake button" would remove the button exactly when
        it becomes useful. Only a positive result writes.
        """
        caps = await self.ssh.async_probe()

        if caps is None:
            agent = await async_probe_hass_agent(
                self.hass, self.worker_id, self.hass_agent_device
            )
            if agent is not None:
                self._hass_agent = agent
                caps = agent.capabilities

        if caps is not None and caps.mac is None:
            mac = await self.hass.async_add_executor_job(
                resolve_mac_via_arp, self.host
            )
            if mac:
                caps = replace(caps, mac=mac)

        if caps is None:
            return

        self.power_capabilities = caps
        self._persist_power_capabilities(caps)

    def _persist_power_capabilities(self, caps: PowerCapabilities) -> None:
        """Store the capabilities in the entry, if they have changed.

        Conditional so as not to rewrite `.storage` on every start-up for
        identical content. The integration deliberately installs no update
        listener (see __init__.py), so this write reloads nothing: it happens
        during setup, and a reload at that moment would loop.
        """
        stored = caps.as_dict()
        if self.entry.data.get(CONF_POWER_CAPS) == stored:
            return
        _LOGGER.debug("Power capabilities of %s remembered: %s", self.host, stored)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_POWER_CAPS: stored}
        )

    async def async_power_command(self, action: str) -> None:
        """Power the machine off, reboot it or suspend it, through whichever source declared it.

        With HASS.Agent there is nothing to catch: the command goes out over
        MQTT and the broker acknowledges it long before the machine starts
        shutting down.

        Over SSH the opposite holds: the shutdown cuts the session while it is
        running, and depending on when systemd kills sshd, asyncssh may raise
        instead of returning cleanly. That error *is* the expected success, so
        it is not propagated -- otherwise Home Assistant would show a failure
        every time the action worked perfectly.

        Suspend is the same shape with a different flavour. `systemctl suspend`
        asks logind and returns before anything happens, so the command usually
        exits 0 and only then does the machine stop -- but if it stops first,
        the session does not close, it *freezes*: it is the machine that went
        away, not the socket, so there is no FIN and the timeout is what ends
        the wait. Both outcomes land in the same except clause below, which is
        why suspend needs no special case here.
        """
        caps = self.power_capabilities

        # An empty stored command means this host was established through
        # HASS.Agent. After a degraded start-up the probe has not run, so
        # _hass_agent is still None while the capabilities say otherwise --
        # look the device up now rather than falling through to an SSH command
        # the host has no way to answer.
        if self._hass_agent is None and caps is not None and not caps.command:
            self._hass_agent = await async_probe_hass_agent(
                self.hass, self.worker_id, self.hass_agent_device
            )
            if self._hass_agent is None:
                raise HomeAssistantError(
                    f"{self.host} is driven through HASS.Agent, whose device"
                    " cannot be found right now"
                )

        if self._hass_agent is not None:
            await self._hass_agent.async_press(self.hass, action)
            return

        command = caps.command if caps and caps.command else RIG_POWER_SUDO
        try:
            await self.ssh.async_run(f"{command} {action}", timeout=20)
        except DISCONNECTED_ERRORS:
            _LOGGER.debug(
                "Session cut during '%s %s' on %s: expected",
                command,
                action,
                self.host,
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"'{command} {action}' failed on {self.host}: {err}"
            ) from err

    async def _async_get_json(self, url: str) -> dict | list:
        async with self.session.get(url, auth=self._glances_auth) as resp:
            resp.raise_for_status()
            # Glances serves JSON as text/html on some versions, hence
            # content_type=None rather than raising on the MIME type.
            return await resp.json(content_type=None)

    async def async_send_command(self, method: str) -> None:
        """Send pause/resume/stop through the json_rpc API, then refresh state.

        A raw HTTP/1.0 client rather than aiohttp: XMRig's server sometimes
        returns a duplicated response ("Data after Connection: close") which
        aiohttp's strict parser rejects even though the command was applied.
        Here the response is read until close and only the 200 status plus an
        "OK" body are checked, the way curl would.
        """
        body = json.dumps({"method": method, "id": 1})
        request = (
            f"POST /json_rpc HTTP/1.0\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Authorization: Bearer {self._token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        ).encode()

        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    writer.write(request)
                    await writer.drain()
                    raw = await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"XMRig unreachable for command {method}: {err}"
            ) from err

        status_line = raw.split(b"\r\n", 1)[0]
        if b" 200 " not in status_line and not status_line.rstrip().endswith(b"200 OK"):
            raise HomeAssistantError(
                f"XMRig refused command {method}: {status_line.decode(errors='replace')}"
            )
        if b'"status"' not in raw or b"OK" not in raw:
            raise HomeAssistantError(
                f"Unexpected XMRig response for command {method}"
            )

        await self.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        """The "Device info" card on the rig's page.

        The OS and the RAM appear there in addition to their sensors. That is
        not pointless duplication: this card does not display entities, only the
        fields below, and it is where one looks to find out *what* a machine is.
        The sensors, meanwhile, keep the history and serve automations. Two
        uses, two places.

        For want of a "system" field in Home Assistant, the OS is appended to
        the XMRig version -- both are software -- and the RAM takes the hardware
        version slot.
        """
        data = self.miner
        cpu = data.get("cpu") or {}

        version = data.get("version")
        os_name = miner_os(data)
        if version and os_name:
            sw_version = f"XMRig {version} ({os_name})"
        elif version:
            sw_version = f"XMRig {version}"
        else:
            sw_version = os_name

        total = miner_memory_total(data)
        hw_version = f"{total / 1024 ** 3:.1f} GiB RAM" if total else None

        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            manufacturer=MANUFACTURER,
            model=cpu.get("brand"),
            sw_version=sw_version,
            hw_version=hw_version,
            configuration_url=self.base_url,
        )
