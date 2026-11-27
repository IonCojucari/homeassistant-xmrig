"""Run/pause switch for an XMRig rig."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XmrigConfigEntry
from .const import STATE_MINING
from .coordinator import XmrigCoordinator
from .fleet import (
    SIGNAL_FLEET_UPDATED,
    FleetEntity,
    Rig,
    is_fleet,
    rig_entries,
    rig_switch_unique_id,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if is_fleet(entry):
        _async_setup_fleet_switches(hass, entry, async_add_entities)
        return
    async_add_entities([XmrigSwitch(entry.runtime_data)])


@callback
def _async_setup_fleet_switches(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One switch per rig on the fleet device, including rigs added later.

    Created for every rig entry, loaded or not, so a rig that is off at start-up
    still has its row on the panel -- unavailable until it answers. A deleted
    rig's switch is removed in fleet.async_setup_fleet.
    """
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [e for e in rig_entries(hass) if e.entry_id not in known]
        if not new:
            return
        known.update(e.entry_id for e in new)
        async_add_entities(XmrigFleetRigSwitch(entry, rig) for rig in new)

    _add_new()
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_FLEET_UPDATED, _add_new)
    )


class XmrigSwitch(CoordinatorEntity[XmrigCoordinator], SwitchEntity):
    """ON = mining, OFF = XMRig paused (the process stays alive)."""

    _attr_has_entity_name = True
    _attr_name = None  # primary entity: takes the device's name
    _attr_icon = "mdi:pickaxe"

    def __init__(self, coordinator: XmrigCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_switch"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return not self.coordinator.miner.get("paused", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("resume")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command("pause")


class XmrigFleetRigSwitch(FleetEntity, SwitchEntity):
    """One rig's run/pause switch, repeated on the fleet device.

    The same command as the switch on the rig's own device, gathered with the
    others so the whole fleet can be run from one panel. Named after the rig,
    and renamed with it.
    """

    _attr_icon = "mdi:pickaxe"

    def __init__(self, entry: ConfigEntry, rig_entry: ConfigEntry) -> None:
        super().__init__(entry, "")
        self._attr_unique_id = rig_switch_unique_id(entry, rig_entry)
        self._rig_entry_id = rig_entry.entry_id
        self._attr_name = rig_entry.title
        self._coordinator: XmrigCoordinator | None = None

    @callback
    def _update(self, rigs: list[Rig]) -> None:
        rig = next((r for r in rigs if r.entry.entry_id == self._rig_entry_id), None)
        self._coordinator = rig.coordinator if rig is not None else None
        if rig is not None:
            self._attr_name = rig.name
        self._attr_available = rig is not None and rig.answering
        self._attr_is_on = rig.status == STATE_MINING if self._attr_available else None

    async def _async_send(self, method: str) -> None:
        if self._coordinator is None:
            raise HomeAssistantError(f"{self.name} is not loaded")
        await self._coordinator.async_send_command(method)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_send("resume")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send("pause")
