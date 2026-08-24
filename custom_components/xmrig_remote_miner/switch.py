"""Run/pause switch for an XMRig rig."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XmrigConfigEntry
from .coordinator import XmrigCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([XmrigSwitch(entry.runtime_data)])


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
