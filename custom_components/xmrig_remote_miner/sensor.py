"""XMRig sensors (derived from the /1/summary payload)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XmrigConfigEntry
from .const import STATE_LOST, STATE_MINING, STATE_OFF, STATE_PAUSED
from .coordinator import XmrigCoordinator, miner_memory_total, miner_os


def _hashrate_khs(data: dict) -> float | None:
    """The current hashrate, in kH/s.

    One unit for the fleet, and this is the readable one: past a thousand
    hashes, "2.08 kH/s" reads at a glance where "2080.4 H/s" makes you count
    digits.
    """
    total = (data.get("hashrate") or {}).get("total") or []
    if total and total[0] is not None:
        return round(float(total[0]) / 1000, 2)
    return None


def _hashrate_max(data: dict) -> float | None:
    """The highest this rig has reached, in the same unit as the live one.

    XMRig reports it in H/s like everything else; converting here keeps every
    hashrate sensor in one unit.
    """
    value = (data.get("hashrate") or {}).get("highest")
    return round(float(value) / 1000, 2) if value is not None else None


def _shares_good(data: dict) -> int | None:
    return (data.get("results") or {}).get("shares_good")


def _shares_rejected(data: dict) -> int | None:
    results = data.get("results") or {}
    total = results.get("shares_total")
    good = results.get("shares_good")
    if total is None or good is None:
        return None
    return total - good


def _pool(data: dict) -> str | None:
    return (data.get("connection") or {}).get("pool")


def _ping(data: dict) -> int | None:
    return (data.get("connection") or {}).get("ping")


def _uptime(data: dict) -> int | None:
    return data.get("uptime")


def _state(data: dict) -> str | None:
    """The miner's state, as an enum key rather than as displayable text.

    A sensor's state is not presentation: it goes into the history, it is used
    as an automation condition, and publishing it translated means a change of
    language breaks automations silently. The displayed labels live under
    `entity.sensor.state.state.{mining,paused}` instead.
    """
    if not data:
        return None
    return STATE_PAUSED if data.get("paused") else STATE_MINING


def _memory_usage(data: dict) -> float | None:
    """RAM in use, as a percentage of the total.

    XMRig takes "free" from libuv, which reads MemAvailable on Linux, so page
    cache counts as free -- as it should, since the kernel hands it back on
    demand.
    """
    memory = (data.get("resources") or {}).get("memory") or {}
    total, free = memory.get("total"), memory.get("free")
    if not total or free is None:
        return None
    return round((total - free) / total * 100, 1)


def _load(data: dict) -> float | None:
    """15-minute load average, relative to the core count.

    Relative, because "4" means nothing on its own: it is saturated on a 4-core
    machine and half idle on an 8-core one. As a percentage, 100% = every core
    busy, which compares directly from one rig to the next.

    None on Windows, which has no load average: libuv reports zeros there, and
    a rig that reads 0% while mining flat out is a wrong but plausible number.
    """
    if (miner_os(data) or "").startswith("Windows"):
        return None
    resources = data.get("resources") or {}
    averages = resources.get("load_average") or []
    cores = resources.get("hardware_concurrency")
    if len(averages) < 3 or not cores:
        return None
    return round(float(averages[2]) / float(cores) * 100, 1)


@dataclass(frozen=True, kw_only=True)
class XmrigSensorDescription(SensorEntityDescription):
    """A sensor description together with its extraction function."""

    value_fn: Callable[[dict], str | int | float | None]


SENSORS: tuple[XmrigSensorDescription, ...] = (
    XmrigSensorDescription(
        key="state",
        translation_key="state",
        icon="mdi:pickaxe",
        device_class=SensorDeviceClass.ENUM,
        options=[STATE_MINING, STATE_PAUSED, STATE_OFF, STATE_LOST],
        value_fn=_state,
    ),
    XmrigSensorDescription(
        key="hashrate_khs",
        translation_key="hashrate_khs",
        native_unit_of_measurement="kH/s",
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
        value_fn=_hashrate_khs,
    ),
    XmrigSensorDescription(
        key="hashrate_max",
        translation_key="hashrate_max",
        native_unit_of_measurement="kH/s",
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer-medium",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_hashrate_max,
    ),
    XmrigSensorDescription(
        key="shares_good",
        translation_key="shares_good",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:check-decagram",
        value_fn=_shares_good,
    ),
    XmrigSensorDescription(
        key="shares_rejected",
        translation_key="shares_rejected",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:close-octagon",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_shares_rejected,
    ),
    XmrigSensorDescription(
        key="uptime",
        translation_key="uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:timer-outline",
        value_fn=_uptime,
    ),
    XmrigSensorDescription(
        key="pool",
        translation_key="pool",
        icon="mdi:server-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_pool,
    ),
    XmrigSensorDescription(
        key="ping",
        translation_key="ping",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:lan-connect",
        value_fn=_ping,
    ),
    XmrigSensorDescription(
        key="os",
        translation_key="os",
        icon="mdi:desktop-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=miner_os,
    ),
    XmrigSensorDescription(
        key="memory_total",
        translation_key="memory_total",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        device_class=SensorDeviceClass.DATA_SIZE,
        icon="mdi:memory",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=miner_memory_total,
    ),
    XmrigSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_memory_usage,
    ),
    XmrigSensorDescription(
        key="load",
        translation_key="load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-line",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_load,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(XmrigSensor(coordinator, desc) for desc in SENSORS)


class XmrigSensor(CoordinatorEntity[XmrigCoordinator], SensorEntity):
    """One sensor, attached to the rig's device."""

    _attr_has_entity_name = True
    entity_description: XmrigSensorDescription

    def __init__(
        self, coordinator: XmrigCoordinator, description: XmrigSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        """Follow the machine's own state entity, for the state sensor only.

        The coordinator is not enough here. It reports the miner, and the miner
        is exactly what is not answering when this matters; a poll that fails
        the same way twice tells the entity nothing new, so nothing rewrites it.

        Meanwhile the machine's word arrives over MQTT as a retained message,
        restored whenever the MQTT integration gets round to it -- which can be
        after this entry has finished setting up. Without this, a Home Assistant
        restarted overnight would show a rig that has been off for hours as
        `unavailable` until the rig came back and proved it, which is the one
        case this sensor was extended for.
        """
        await super().async_added_to_hass()
        if self.entity_description.key != "state":
            return
        if (entity_id := self.coordinator.machine_entity) is None:
            return
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [entity_id], self._machine_said_something
            )
        )

    @callback
    def _machine_said_something(self, event) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not super().available:
            # One exception, and it is the whole point of reading MQTT: when
            # the miner stops answering because the machine is off, this sensor
            # is the one entity that still has something true to say. Going
            # unavailable here would make "switched off as asked" and "fell
            # over" look identical -- both would simply stop reporting.
            return (
                self.entity_description.key == "state"
                and self.coordinator.machine_state is not None
            )
        return True

    @property
    def native_value(self) -> str | int | float | None:
        # The machine's own word wins whenever XMRig is not answering, and only
        # then: a running miner is the better authority on whether it is paused.
        # Guarded on the key as well as on the poll, because every other sensor
        # here reports a number and would be handed a word.
        if (
            self.entity_description.key == "state"
            and not self.coordinator.last_update_success
        ):
            return self.coordinator.machine_state
        return self.entity_description.value_fn(self.coordinator.miner)
