"""The fleet: every rig at once.

Each rig is its own config entry, so nothing that belongs to one of them can
speak for all of them. The fleet is therefore an entry of its own -- offered
automatically as a discovered card as soon as a rig is set up -- carrying the
entities that only make sense across rigs: totals, counts, the pause-all and
resume-all buttons, and a pause switch per rig gathered on one device.

It owns nothing. Membership comes from the rig config entries themselves, so a
rig that is set up but not answering, or stuck retrying its setup, still counts
as a rig. The live data comes from the coordinators the loaded rigs register
here. A rig added or removed later simply joins or leaves the fleet, without
the fleet entry having to be reloaded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    SOURCE_INTEGRATION_DISCOVERY,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import discovery_flow, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    FLEET_UNIQUE_ID,
    MANUFACTURER,
    STATE_LOST,
    STATE_MINING,
    STATE_OFF,
    STATE_PAUSED,
)
from .coordinator import XmrigCoordinator

_LOGGER = logging.getLogger(__name__)

# The loaded rigs, by entry_id. Filled by the rigs themselves, not looked up in
# the config entries: an entry is only LOADED once its setup has returned, which
# is after its first poll has already been announced.
RIGS: HassKey[dict[str, XmrigCoordinator]] = HassKey(f"{DOMAIN}_rigs")

# Sent whenever a rig finishes a poll, or any entry of this domain changes:
# added, removed, loaded, unloaded, retrying.
SIGNAL_FLEET_UPDATED = f"{DOMAIN}_fleet_updated"

# A rig between two states. Seen at start-up, before a rig's setup has run, and
# for the few seconds of every reload. The fleet's figures are not recomputed
# while any rig is in one of these, so a reload does not show as a dip and a
# restart does not show as a staircase climbing back to the real total.
_TRANSITIONAL = {
    ConfigEntryState.NOT_LOADED,
    ConfigEntryState.SETUP_IN_PROGRESS,
    ConfigEntryState.UNLOAD_IN_PROGRESS,
}

# What a rig is doing, as the fleet reports it.
STATUS_NOT_ANSWERING = "not_answering"


def is_fleet(entry: ConfigEntry) -> bool:
    return entry.unique_id == FLEET_UNIQUE_ID


def rigs(hass: HomeAssistant) -> dict[str, XmrigCoordinator]:
    return hass.data.get(RIGS, {})


def rig_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every rig the user has set up and not disabled, sorted by name."""
    return sorted(
        (
            e
            for e in hass.config_entries.async_entries(
                DOMAIN, include_ignore=False, include_disabled=False
            )
            if not is_fleet(e)
        ),
        key=lambda e: e.title.casefold(),
    )


@dataclass(frozen=True)
class Rig:
    """One rig as the fleet sees it: its entry, and its coordinator if loaded."""

    entry: ConfigEntry
    coordinator: XmrigCoordinator | None

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def answering(self) -> bool:
        return self.coordinator is not None and self.coordinator.last_update_success

    @property
    def status(self) -> str:
        """mining, paused, off, lost, or not_answering.

        Off and lost come from the machine's own MQTT device, the same as the
        rig's state sensor, and only when XMRig is not answering.
        """
        if self.answering:
            return STATE_PAUSED if self.coordinator.miner.get("paused") else STATE_MINING
        if self.coordinator is not None and (
            machine := self.coordinator.machine_state
        ):
            return machine
        return STATUS_NOT_ANSWERING

    @property
    def summary(self) -> dict:
        """The last summary, kept after a failed poll; empty if never polled."""
        return self.coordinator.miner if self.coordinator is not None else {}


def fleet(hass: HomeAssistant) -> list[Rig] | None:
    """Every rig, or None while one of them is between two states."""
    entries = rig_entries(hass)
    if any(e.state in _TRANSITIONAL for e in entries):
        return None
    loaded = rigs(hass)
    return [Rig(e, loaded.get(e.entry_id)) for e in entries]


@callback
def async_register_rig(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: XmrigCoordinator
) -> None:
    """Hand the fleet a rig's live data for as long as its entry is loaded."""
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
    async_offer_fleet(hass)


@callback
def async_offer_fleet(hass: HomeAssistant) -> None:
    """Offer the fleet entry if there is none and at least one rig.

    A discovered card rather than an entry created behind the user's back: it
    takes one click to accept, and one to ignore for good. The flow aborts by
    itself if an offer is already pending.
    """
    if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, FLEET_UNIQUE_ID):
        return
    if not rig_entries(hass):
        return
    discovery_flow.async_create_flow(
        hass,
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={},
    )


@callback
def async_setup_fleet(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """What the fleet entry listens to, besides the rigs' polls."""

    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if changed.domain != DOMAIN or is_fleet(changed):
            return
        # A deleted rig takes its own entities with it, but its switch on the
        # fleet device belongs to the fleet entry and has to be removed here.
        if change is ConfigEntryChange.REMOVED:
            registry = er.async_get(hass)
            if entity_id := registry.async_get_entity_id(
                "switch", DOMAIN, rig_switch_unique_id(entry, changed)
            ):
                registry.async_remove(entity_id)
        async_dispatcher_send(hass, SIGNAL_FLEET_UPDATED)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed)
    )


@callback
def async_setup_rediscovery(hass: HomeAssistant) -> None:
    """Offer the fleet again once its entry, or the ignore of it, is removed.

    Without this the offer would only come back the next time a rig sets up,
    i.e. at the next restart: deleting the fleet, or clicking "stop ignoring"
    on it, would otherwise appear to do nothing.
    """

    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if (
            change is ConfigEntryChange.REMOVED
            and changed.domain == DOMAIN
            and is_fleet(changed)
        ):
            async_offer_fleet(hass)

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed)


def rig_switch_unique_id(fleet_entry: ConfigEntry, rig_entry: ConfigEntry) -> str:
    return f"{fleet_entry.entry_id}_rig_{rig_entry.entry_id}"


def fleet_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, FLEET_UNIQUE_ID)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="Fleet",
        entry_type=DeviceEntryType.SERVICE,
    )


class FleetEntity(Entity):
    """An entity on the fleet device, recomputed whenever the fleet changes.

    The figures are only recomputed while every rig is in a settled state.
    During a rig's reload the entity keeps showing what it last showed; at
    start-up, until every rig has had its setup attempt, it shows unknown.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = fleet_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_FLEET_UPDATED, self._refresh)
        )
        # Written by Home Assistant right after this returns.
        if (current := fleet(self.hass)) is not None:
            self._update(current)

    @callback
    def _refresh(self) -> None:
        if (current := fleet(self.hass)) is None:
            return
        self._update(current)
        self.async_write_ha_state()

    @callback
    def _update(self, rigs: list[Rig]) -> None:
        """Set the entity's attributes from the current rigs."""
        raise NotImplementedError


async def async_command_all(hass: HomeAssistant, method: str) -> None:
    """Send pause or resume to every rig that could be mining.

    The only rigs skipped are the ones whose machine has said it is off, or
    whose MQTT connection died without a goodbye: neither is running XMRig for
    anyone. Every other rig is tried, including one whose last poll failed --
    a single timed-out poll does not stop a miner hashing, and skipping it
    would let "pause all" succeed while that rig keeps drawing full power.

    The rigs are commanded together, and one refusing does not stop the rest:
    the press does everything it can, then names every rig it could not reach,
    including any whose entry is not loaded at all.
    """
    all_rigs = fleet(hass)
    if all_rigs is None:
        raise HomeAssistantError("A rig is being set up or reloaded; try again")

    targets: list[Rig] = []
    failed: list[str] = []
    for rig in all_rigs:
        if rig.status in (STATE_OFF, STATE_LOST):
            _LOGGER.debug("%s skips %s: the machine is %s", method, rig.name, rig.status)
        elif rig.coordinator is None:
            failed.append(f"{rig.name} (not loaded)")
        else:
            targets.append(rig)

    results = await asyncio.gather(
        *(rig.coordinator.async_send_command(method) for rig in targets),
        return_exceptions=True,
    )
    failed += [
        f"{rig.name} ({result})"
        for rig, result in zip(targets, results)
        if isinstance(result, BaseException)
    ]
    if failed:
        raise HomeAssistantError(
            f"{method} did not reach {len(failed)} of {len(all_rigs)} rigs: "
            + "; ".join(failed)
        )
