r"""Power control for a machine that already goes through HASS.Agent.

The second source of host control, after SSH. It exists for Windows machines:
they have neither `sudo` nor `rig-power`, and giving them the equivalent means
installing OpenSSH Server, placing the key in the right spot -- for an
administrator account that is not `~/.ssh/authorized_keys` but
`C:\ProgramData\ssh\administrators_authorized_keys` -- and then writing a
wrapper. If HASS.Agent already runs there, all of that exists by another route.

So nothing is reimplemented: HASS.Agent publishes its commands over MQTT, Home
Assistant has already turned them into entities, and those get pressed. This
module talks only to Home Assistant's registries, never to the network.

Those entities are `button`s today and `switch`es on older HASS.Agent versions.
Both are accepted: accepting only one form fails exactly like a missing command
-- device found, no buttons created, and nothing to say why.

Matching is deterministic on both sides. HASS.Agent registers with
`Identifiers = "hass.agent-" + name` (CreateDeviceConfigModel), and XMRig
publishes `worker_id` in its summary, which defaults to the machine name. When
the two have drifted apart, the config field decides.

An accepted consequence: the shutdown entities then exist twice, HASS.Agent's
own and the buttons created here. That is the price of one device per rig in
Home Assistant -- the same trade-off as reading Glances directly instead of
adding the official integration alongside.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    ACTION_OFF,
    ACTION_REBOOT,
    ACTION_SUSPEND,
    HASS_AGENT_IDENTIFIER_PREFIX,
    HASS_AGENT_REBOOT_KEYS,
    HASS_AGENT_SHUTDOWN_KEYS,
    HASS_AGENT_SUSPEND_KEYS,
    MQTT_DOMAIN,
)
from .ssh import PowerCapabilities

_LOGGER = logging.getLogger(__name__)

# HASS.Agent publishes its commands as `button`s, and older versions did so as
# `switch`es. Both are accepted, because an integration that only accepts
# yesterday's form fails exactly like a command that was never created --
# silently, with the device plainly present and no button facing it. That is the
# defect this table fixes.
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


@dataclass(frozen=True)
class HassAgentPower:
    """The HASS.Agent entities selected for this rig."""

    device_id: str
    # action -> (domain, entity_id). The domain is kept rather than guessed
    # again later: it decides which service is called, and getting it wrong only
    # shows up at the moment someone wants to power a machine off.
    entities: dict[str, tuple[str, str]]

    @property
    def capabilities(self) -> PowerCapabilities:
        """The same capabilities the SSH probe returns, so callers ignore the source.

        `mac` stays None: HASS.Agent does not fill in `Connections` on its
        device, so there is no MAC to take from it. Waking goes through the ARP
        fallback, as for any machine that cannot name its own.
        """
        return PowerCapabilities(
            actions=frozenset(self.entities), mac=None, command=""
        )

    async def async_press(self, hass: HomeAssistant, action: str) -> None:
        """Trigger the matching action on the HASS.Agent side."""
        target = self.entities.get(action)
        if target is None:
            raise HomeAssistantError(
                f"HASS.Agent does not expose the action '{action}' for this device"
            )
        domain, entity_id = target
        _LOGGER.debug("HASS.Agent: %s -> %s.%s", action, domain, entity_id)
        await hass.services.async_call(
            domain,
            _COMMAND_SERVICES[domain],
            {"entity_id": entity_id},
            blocking=True,
        )


def _find_device(
    hass: HomeAssistant, worker_id: str | None, device_id: str | None
) -> str | None:
    """Find the HASS.Agent device, by explicit choice or by worker_id."""
    registry = dr.async_get(hass)

    if device_id:
        device = registry.async_get(device_id)
        if device is None:
            _LOGGER.debug("HASS.Agent device %s not found", device_id)
        return device.id if device else None

    if not worker_id:
        return None

    device = registry.async_get_device(
        identifiers={(MQTT_DOMAIN, f"{HASS_AGENT_IDENTIFIER_PREFIX}{worker_id}")}
    )
    return device.id if device else None


def _find_commands(
    hass: HomeAssistant, device_id: str
) -> dict[str, tuple[str, str]]:
    """Map each action to the HASS.Agent entity that performs it.

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
        if any(key in haystack for key in HASS_AGENT_SHUTDOWN_KEYS):
            found.setdefault(ACTION_OFF, (entry.domain, entry.entity_id))
        elif any(key in haystack for key in HASS_AGENT_REBOOT_KEYS):
            found.setdefault(ACTION_REBOOT, (entry.domain, entry.entity_id))
        elif any(key in haystack for key in HASS_AGENT_SUSPEND_KEYS):
            found.setdefault(ACTION_SUSPEND, (entry.domain, entry.entity_id))

    return found


async def async_probe(
    hass: HomeAssistant, worker_id: str | None, device_id: str | None = None
) -> HassAgentPower | None:
    """Look for a way to power this machine off through HASS.Agent. None = none.

    A device found with no shutdown command is the common case, not an anomaly:
    HASS.Agent does not create Shutdown and Restart by default, they have to be
    added in its Commands tab. None is returned rather than buttons that would
    do nothing.
    """
    device_id = _find_device(hass, worker_id, device_id)
    if device_id is None:
        return None

    entities = _find_commands(hass, device_id)
    if not entities:
        _LOGGER.debug(
            "HASS.Agent device %s found, but no shutdown command:"
            " add Shutdown/Restart in HASS.Agent's Commands tab",
            device_id,
        )
        return None

    _LOGGER.debug("HASS.Agent %s: %s", device_id, entities)
    return HassAgentPower(device_id=device_id, entities=entities)
