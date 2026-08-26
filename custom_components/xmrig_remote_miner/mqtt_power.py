"""Power control for a machine through the MQTT device it publishes itself.

The single mechanism for host power. The machine -- HASS.Agent on Windows, the
rig agent on NixOS -- announces its own entities over MQTT discovery, Home
Assistant has already turned them into a device with buttons, and this module
presses them. Nothing is reimplemented and nothing is executed remotely: this
code talks only to Home Assistant's registries, never to the network.

Both families are handled by the same lookup and the same table, because both
publish the same command names. That is the point: one code path, whatever the
operating system on the other end.

An accepted consequence: the shutdown entities then exist twice, the machine's
own and the buttons created here. That is the price of one device per rig in
Home Assistant -- the same trade-off as reading Glances directly instead of
adding the official integration alongside.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .const import (
    ACTION_OFF,
    ACTION_REBOOT,
    ACTION_SUSPEND,
    CAPS_ACTIONS,
    CAPS_MAC,
    DEVICE_IDENTIFIER_PREFIXES,
    MQTT_DOMAIN,
    POWER_REBOOT_KEYS,
    POWER_SHUTDOWN_KEYS,
    POWER_SUSPEND_KEYS,
    WOL_PORT,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)

# The command entities are `button`s in current agents and `switch`es in older
# HASS.Agent versions. Both are accepted, because an integration that only
# accepts today's form fails exactly like a command that was never created --
# silently, with the device plainly present and no button facing it.
#
# The service to call is not the same:
#   button  -> press
#   switch  -> turn_on, and never toggle. A ShutdownCommand exposed as a switch
#              initialises to "OFF" and runs `shutdown /s` on the transition to
#              ON; since its state never returns to OFF, a toggle would power
#              the machine off exactly once.
_COMMAND_SERVICES = {
    "button": "press",
    "switch": "turn_on",
}


def _clean_mac(mac: object) -> str | None:
    """A lowercase colon-separated MAC, or None for anything else.

    Three sources feed this -- the device registry, the config field and the
    entry written by an earlier version -- and none of them is validated
    anywhere else, so the check lives here once rather than three times.
    """
    if not isinstance(mac, str):
        return None
    mac = mac.strip().replace("-", ":").lower()
    return mac if _MAC_RE.match(mac) else None


@dataclass(frozen=True)
class PowerCapabilities:
    """What this machine accepts, as it declared itself.

    Stored in the config entry rather than only in memory: see CONF_POWER_CAPS.
    """

    actions: frozenset[str]
    mac: str | None

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
        return {CAPS_ACTIONS: sorted(self.actions), CAPS_MAC: self.mac}

    @classmethod
    def from_dict(cls, data: dict | None) -> PowerCapabilities | None:
        """Read back what `as_dict` wrote. None if nothing usable.

        Tolerant by construction, and it has to be: this data was written by an
        earlier version of the integration and lives in entries that exist in
        production. A missing field, one of an unexpected type, or a key that no
        longer means anything -- the `command` that used to tell SSH from
        HASS.Agent -- must yield capabilities all the same, never an exception
        that would stop the entry loading and never a button that disappears.
        Unknown keys are ignored by simply not being read.
        """
        if not isinstance(data, dict):
            return None
        actions = data.get(CAPS_ACTIONS)
        if not isinstance(actions, (list, tuple, set, frozenset)):
            actions = ()

        caps = cls(
            actions=frozenset(
                a
                for a in actions
                if a in (ACTION_OFF, ACTION_REBOOT, ACTION_SUSPEND)
            ),
            mac=_clean_mac(data.get(CAPS_MAC)),
        )
        return caps if caps.any else None


@dataclass(frozen=True)
class MqttPower:
    """The MQTT entities selected for this rig, and its MAC."""

    # action -> (domain, entity_id). The domain is kept rather than guessed
    # again later: it decides which service is called, and getting it wrong only
    # shows up at the moment someone wants to power a machine off.
    entities: dict[str, tuple[str, str]]
    mac: str | None

    @property
    def capabilities(self) -> PowerCapabilities:
        return PowerCapabilities(actions=frozenset(self.entities), mac=self.mac)

    async def async_press(self, hass: HomeAssistant, action: str) -> None:
        """Trigger the matching action on the machine's own entity."""
        target = self.entities.get(action)
        if target is None:
            raise HomeAssistantError(
                f"this machine does not expose the action '{action}'"
            )
        domain, entity_id = target
        _LOGGER.debug("MQTT power: %s -> %s.%s", action, domain, entity_id)
        await hass.services.async_call(
            domain,
            _COMMAND_SERVICES[domain],
            {"entity_id": entity_id},
            blocking=True,
        )


def _find_device(
    hass: HomeAssistant, worker_id: str | None, device_id: str | None
) -> dr.DeviceEntry | None:
    """Find the machine's MQTT device, by explicit choice or by worker_id."""
    registry = dr.async_get(hass)

    if device_id:
        device = registry.async_get(device_id)
        if device is None:
            _LOGGER.debug("MQTT device %s not found", device_id)
        return device

    if not worker_id:
        return None

    for prefix in DEVICE_IDENTIFIER_PREFIXES:
        device = registry.async_get_device(
            identifiers={(MQTT_DOMAIN, f"{prefix}{worker_id}")}
        )
        if device is not None:
            return device
    return None


def _device_mac(device: dr.DeviceEntry | None) -> str | None:
    """The MAC the device declared, for Wake-on-LAN.

    The rig agent publishes it as `cns` in its discovery payload, which Home
    Assistant stores as a (mac, …) connection. HASS.Agent declares no
    connections at all, so a Windows host yields None here and falls back to the
    config field.
    """
    if device is None:
        return None
    return next(
        (
            mac
            for kind, value in device.connections
            if kind == CONNECTION_NETWORK_MAC and (mac := _clean_mac(value))
        ),
        None,
    )


def _find_commands(
    hass: HomeAssistant, device_id: str
) -> dict[str, tuple[str, str]]:
    """Map each action to the entity that performs it.

    The registries are read on every probe rather than an entity_id being
    remembered: renaming an entity in Home Assistant must not break the button.

    Matching is done on the name, unique_id included, because HASS.Agent lets
    the user name their own commands. The identifiers it generates are GUIDs, so
    it is the entity_id that carries the useful word.
    """
    registry = er.async_get(hass)
    found: dict[str, tuple[str, str]] = {}

    for entry in er.async_entries_for_device(registry, device_id):
        if entry.domain not in _COMMAND_SERVICES:
            continue
        haystack = f"{entry.unique_id or ''} {entry.entity_id}".lower()
        if any(key in haystack for key in POWER_SHUTDOWN_KEYS):
            found.setdefault(ACTION_OFF, (entry.domain, entry.entity_id))
        elif any(key in haystack for key in POWER_REBOOT_KEYS):
            found.setdefault(ACTION_REBOOT, (entry.domain, entry.entity_id))
        elif any(key in haystack for key in POWER_SUSPEND_KEYS):
            found.setdefault(ACTION_SUSPEND, (entry.domain, entry.entity_id))

    return found


# Not a coroutine, and it never was one in substance: the whole probe is two
# registry lookups. The `async_` prefix and the @callback decorator are Home
# Assistant's way of saying "call me from the event loop", which is exactly what
# this does.
@callback
def async_probe(
    hass: HomeAssistant,
    worker_id: str | None,
    device_id: str | None = None,
    fallback_mac: str | None = None,
) -> MqttPower | None:
    """Look for a way to power this machine. None = none.

    Nothing is ever invented here. Capabilities come from a device the machine
    itself published under its own name, so a host running no agent -- the
    Home Assistant server itself, for one, whose miner is an add-on rather than
    a remote machine -- matches nothing and gains no buttons. That is the whole
    safeguard, and it is why the lookup is by identifier and not by address.

    A device found with no command entity is a normal case, not an anomaly:
    HASS.Agent does not create Shutdown and Restart by default, they have to be
    added in its Commands tab, and a rig only publishes the buttons its own
    config allows. What comes back then is either wake alone, if a MAC is known,
    or nothing -- rather than buttons that would do nothing.

    `fallback_mac` is what to use when the device names no MAC of its own: the
    config field, or the MAC already remembered for this machine. It is a
    fallback and not an override because a device that declares its MAC read it
    from the card holding its default route, which a hand-typed field cannot do
    and a remembered value cannot notice has changed.
    """
    device = _find_device(hass, worker_id, device_id)
    entities = _find_commands(hass, device.id) if device is not None else {}
    mac = _device_mac(device) or _clean_mac(fallback_mac)

    power = MqttPower(entities=entities, mac=mac)
    if not power.capabilities.any:
        _LOGGER.debug(
            "No MQTT power for worker %s: no device, no command entity and no"
            " MAC",
            worker_id,
        )
        return None

    _LOGGER.debug("MQTT power for %s: %s mac=%s", worker_id, entities, mac)
    return power


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Wake the machine. Blocking, so run it in an executor.

    No dependency needed: a magic packet is six 0xFF bytes followed by sixteen
    repetitions of the MAC, sent as a UDP broadcast. Waking is the one action
    that cannot go through MQTT -- a machine that is off runs no client -- hence
    this second mechanism, unrelated to the first.
    """
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, WOL_PORT))
