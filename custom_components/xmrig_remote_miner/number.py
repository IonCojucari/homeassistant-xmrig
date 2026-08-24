"""Mining thread count for an XMRig rig."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
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
    async_add_entities([XmrigThreadsNumber(entry.runtime_data)])


class XmrigThreadsNumber(CoordinatorEntity[XmrigCoordinator], NumberEntity):
    """How many CPU threads the miner is allowed to use.

    A thread count rather than the percentage XMRig actually takes: the
    percentage is of the machine's logical CPUs and then gets capped by the L3
    cache, so on a typical rig everything from 50% upwards means the same thing
    and the top half of the control does nothing. The conversion to the hint is
    done in the coordinator, which leaves XMRig's auto-config in charge of what
    a thread count really implies -- affinity, intensity, cache ceiling.

    Applied live: the miner keeps its pool connection and its RandomX dataset
    across the change, which takes a few milliseconds. How long it lasts is a
    property of the miner, not of this entity -- XMRig applies a config change
    by saving it over the file it was started from, so the value survives a
    restart unless that file is regenerated at every start, which is what both
    the NixOS rigs and the HAOS add-on do. The README says which is which.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "mining_threads"
    _attr_icon = "mdi:cpu-64-bit"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(self, coordinator: XmrigCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_mining_threads"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        # Both ends have to be known for the control to mean anything: without
        # the ceiling it would offer values the miner silently ignores.
        return (
            super().available
            and self.coordinator.mining_threads is not None
            and self.coordinator.max_mining_threads is not None
        )

    @property
    def native_max_value(self) -> float:
        # 1 while the machine has not said yet, which only shows through
        # `available` above -- never as a slider that can be dragged nowhere.
        return float(self.coordinator.max_mining_threads or 1)

    @property
    def native_value(self) -> float | None:
        threads = self.coordinator.mining_threads
        return None if threads is None else float(threads)

    async def async_set_native_value(self, value: float) -> None:
        # round, not int: Home Assistant does not snap an incoming value to
        # native_step, so number.set_value called from an automation with 3.6
        # would truncate to 3 threads where the control shows steps of one.
        await self.coordinator.async_set_mining_threads(round(value))
