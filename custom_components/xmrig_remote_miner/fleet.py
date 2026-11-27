"""The fleet: every rig at once.

Each rig is its own config entry, so nothing that belongs to one of them can
speak for all of them. The fleet is therefore an entry of its own -- created
once, offered automatically as a discovered card as soon as a rig is set up --
carrying the entities that only make sense across rigs: the total hashrate and
the pause-all / resume-all buttons.

It owns nothing. The rigs register their coordinators here while they are
loaded and announce every poll, and the fleet's entities read whatever is
registered at that moment. A rig added or removed later simply joins or leaves
the sum, without the fleet entry having to be reloaded.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, FLEET_UNIQUE_ID, MANUFACTURER
from .coordinator import XmrigCoordinator

_LOGGER = logging.getLogger(__name__)

# The loaded rigs, by entry_id. Filled by the rigs themselves, not looked up in
# the config entries: an entry is only LOADED once its setup has returned, which
# is after its first poll has already been announced.
RIGS: HassKey[dict[str, XmrigCoordinator]] = HassKey(f"{DOMAIN}_rigs")

# Sent whenever the set of rigs changes or any rig finishes a poll.
SIGNAL_FLEET_UPDATED = f"{DOMAIN}_fleet_updated"


def is_fleet(entry: ConfigEntry) -> bool:
    return entry.unique_id == FLEET_UNIQUE_ID


def rigs(hass: HomeAssistant) -> dict[str, XmrigCoordinator]:
    return hass.data.get(RIGS, {})


@callback
def async_register_rig(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: XmrigCoordinator
) -> None:
    """Make a rig count towards the fleet for as long as its entry is loaded."""
    hass.data.setdefault(RIGS, {})[entry.entry_id] = coordinator

    @callback
    def _announce() -> None:
        async_dispatcher_send(hass, SIGNAL_FLEET_UPDATED)

    @callback
    def _unregister() -> None:
        rigs(hass).pop(entry.entry_id, None)
        _announce()

    entry.async_on_unload(coordinator.async_add_listener(_announce))
    entry.async_on_unload(_unregister)
    _announce()

    # Offer the fleet entry if there is none yet. A discovered card rather than
    # an entry created behind the user's back: it takes one click to accept,
    # and one to ignore for good.
    if hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, FLEET_UNIQUE_ID
    ) is None:
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={},
        )


def fleet_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, FLEET_UNIQUE_ID)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="Fleet",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_command_all(hass: HomeAssistant, method: str) -> None:
    """Send pause or resume to every rig that is answering.

    A rig whose last poll failed is skipped rather than tried. It is usually
    one that is switched off, which no pause or resume can change, and trying
    it would hold the press for the full connection timeout and then report
    as a failure what is really the expected state of a stopped machine.

    The others are commanded together, and one rig refusing does not stop the
    rest: the press still does everything it can, then says which rigs did not
    follow.
    """
    targets = [c for c in rigs(hass).values() if c.last_update_success]
    if not targets:
        raise HomeAssistantError("No XMRig rig is answering right now")

    skipped = [c.entry.title for c in rigs(hass).values() if not c.last_update_success]
    if skipped:
        _LOGGER.debug("%s skipped, not answering: %s", method, ", ".join(skipped))

    results = await asyncio.gather(
        *(c.async_send_command(method) for c in targets), return_exceptions=True
    )
    failed = [
        f"{c.entry.title} ({result})"
        for c, result in zip(targets, results)
        if isinstance(result, BaseException)
    ]
    if failed:
        raise HomeAssistantError(
            f"{method} failed on {len(failed)} of {len(targets)} rigs: "
            + "; ".join(failed)
        )
