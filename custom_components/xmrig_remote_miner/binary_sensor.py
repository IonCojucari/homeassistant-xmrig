"""Whether the rig has finished starting and is actually working.

Separate from the `state` sensor because it answers a different question, and
one that automations ask constantly: not "what is this machine doing" but "may I
count on it now". Solar Miner reads exactly this to decide that a load it woke
has finished warming up, instead of watching a wattmeter climb and guessing.

Two sources, in that order:

The machine's own word, where there is an agent to publish it. That agent
watches the miner from the same box every few seconds and keeps saying so
whether or not this integration's poll succeeded, which makes it the better
authority.

Otherwise the XMRig summary, computed here with the same rule the agent uses --
for a machine with no agent, which is Windows through HASS.Agent and the add-on
running on the Home Assistant box itself. Those two would otherwise be the only
loads left without the sensor, and they are the ones whose consumption is
guessed rather than metered.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XmrigConfigEntry
from .coordinator import XmrigCoordinator


def _ready_from_summary(data: dict) -> bool:
    """The agent's rule, applied to the summary this integration already polls.

    A non-zero 60-second average says the climb began; the 10-second average no
    longer running ahead of it says the climb is over. RandomX spends a minute
    or two finishing its dataset while the draw rises, so a first hash means the
    machine booted -- not that its numbers are worth believing yet.
    """
    total = (data.get("hashrate") or {}).get("total") or []
    if len(total) < 2 or total[0] is None or total[1] is None:
        return False
    h10, h60 = float(total[0]), float(total[1])
    return h60 > 0 and h10 <= h60 * 1.1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([XmrigReady(entry.runtime_data)])


class XmrigReady(CoordinatorEntity[XmrigCoordinator], BinarySensorEntity):
    """One per rig: is this machine working right now."""

    _attr_has_entity_name = True
    _attr_translation_key = "ready"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: XmrigCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_ready"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Available while the machine is saying something, poll or no poll.

        A rig that is off is not unknown -- it is not ready, and saying so is
        the whole point. Only a machine that has neither answered nor published
        anything leaves this unavailable.
        """
        return super().available or self.coordinator.machine_ready is not None

    @property
    def is_on(self) -> bool | None:
        if (said := self.coordinator.machine_ready) is not None:
            return said
        if not self.coordinator.last_update_success:
            return None
        return _ready_from_summary(self.coordinator.miner)
