"""Power buttons for the host machine.

Created only for the actions the machine's own MQTT device announced, on the
same principle as the Glances sensors: a missing capability yields fewer
entities, never inert ones.

These are buttons rather than a switch because neither half reports state: a
machine that is off publishes nothing, and a magic packet is never
acknowledged. A switch would promise a state it cannot read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import XmrigConfigEntry
from .coordinator import XmrigCoordinator
from .mqtt_power import send_magic_packet

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class XmrigButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[XmrigCoordinator], Coroutine[Any, Any, None]]


async def _press_off(coordinator: XmrigCoordinator) -> None:
    await coordinator.async_power_command("off")


async def _press_reboot(coordinator: XmrigCoordinator) -> None:
    await coordinator.async_power_command("reboot")


async def _press_suspend(coordinator: XmrigCoordinator) -> None:
    await coordinator.async_power_command("suspend")


async def _press_wake(coordinator: XmrigCoordinator) -> None:
    caps = coordinator.power_capabilities
    if caps is None or caps.mac is None:
        raise HomeAssistantError("no MAC address known for this rig")
    await coordinator.hass.async_add_executor_job(send_magic_packet, caps.mac)
    _LOGGER.debug("Magic packet sent to %s", caps.mac)


SHUTDOWN = XmrigButtonDescription(
    key="shutdown",
    translation_key="shutdown",
    icon="mdi:power",
    entity_category=EntityCategory.CONFIG,
    press_fn=_press_off,
)

REBOOT = XmrigButtonDescription(
    key="reboot",
    translation_key="reboot",
    icon="mdi:restart",
    entity_category=EntityCategory.CONFIG,
    press_fn=_press_reboot,
)

# Suspend rather than shut down, for a rig that is stopped and restarted through
# the day. The machine wakes on the same magic packet as the Wake button below,
# and comes back with the RandomX dataset and the hugepage pool still allocated
# -- which is the difference between resuming in seconds and paying a full boot
# plus a dataset init every time the sun goes behind a cloud.
#
# It only appears when the machine publishes a Sleep button, i.e. when the rig's
# own config has declared that this board's firmware was actually tested: a
# board that sleeps and does not wake is not recoverable remotely, and the Wake
# button would be pressing against nothing.
SUSPEND = XmrigButtonDescription(
    key="suspend",
    translation_key="suspend",
    icon="mdi:sleep",
    entity_category=EntityCategory.CONFIG,
    press_fn=_press_suspend,
)

WAKE = XmrigButtonDescription(
    key="wake",
    translation_key="wake",
    icon="mdi:power-sleep",
    entity_category=EntityCategory.CONFIG,
    press_fn=_press_wake,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    caps = coordinator.power_capabilities
    if caps is None:
        return

    descriptions = []
    if caps.can_power_off:
        descriptions.append(SHUTDOWN)
    if caps.can_reboot:
        descriptions.append(REBOOT)
    if caps.can_suspend:
        descriptions.append(SUSPEND)
    if caps.can_wake:
        descriptions.append(WAKE)

    async_add_entities(XmrigButton(coordinator, d) for d in descriptions)


class XmrigButton(ButtonEntity):
    """One power action, attached to the rig's device."""

    _attr_has_entity_name = True
    entity_description: XmrigButtonDescription

    def __init__(
        self, coordinator: XmrigCoordinator, description: XmrigButtonDescription
    ) -> None:
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Always available, even when the miner is unreachable.

        Deliberate, and the whole point of the wake button: it is only ever
        wanted while the machine is off, i.e. at exactly the moment the
        coordinator is failing. Following the miner's availability would make
        the button unusable precisely when it is needed.
        """
        return True

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
