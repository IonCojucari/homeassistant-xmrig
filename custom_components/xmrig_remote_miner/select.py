"""Pool selection for an XMRig rig.

Created only when the entry lists more than one pool, on the same principle as
the Glances sensors and the power buttons: a missing capability yields fewer
entities, never inert ones. A rig with one pool has nothing to choose between,
and a select with a single option is a control that cannot control anything.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XmrigConfigEntry
from .const import CONF_POOLS
from .coordinator import XmrigCoordinator
from .pools import Pool, normalize, parse

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: XmrigConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    pools = parse(entry.data.get(CONF_POOLS))
    if len(pools) < 2:
        return
    async_add_entities([XmrigPoolSelect(entry.runtime_data, pools)])


class XmrigPoolSelect(CoordinatorEntity[XmrigCoordinator], SelectEntity):
    """Which of the configured pools this rig mines to."""

    _attr_has_entity_name = True
    _attr_translation_key = "pool"
    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, coordinator: XmrigCoordinator, pools: list[Pool]) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_pool_select"
        self._attr_device_info = coordinator.device_info
        self._pools = pools
        self._by_label = {pool.label: pool for pool in pools}
        self._by_key = {pool.key: pool for pool in pools}
        # What was asked for but is not confirmed yet. A pool change takes a
        # reconnect, during which XMRig reports either the old pool or none at
        # all; without this the control would visibly snap back to where it was
        # and look like it had failed.
        self._pending: Pool | None = None
        self._pending_polls = 0

    @property
    def options(self) -> list[str]:
        """The configured pools, plus the live one when it is not among them.

        A rig repointed by hand -- or one whose flake still names a pool that
        was later dropped from this list -- would otherwise put the select in a
        state it cannot describe, which Home Assistant logs as an error on
        every poll. Showing where the rig actually is says more than showing
        nothing.
        """
        options = [pool.label for pool in self._pools]
        if (current := self._live) is not None and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        if self._pending is not None:
            return self._pending.label
        return self._live

    @property
    def _live(self) -> str | None:
        """The pool XMRig reports, named as it is named in the list."""
        pool = self.coordinator.pool
        if not pool:
            return None
        known = self._by_key.get(normalize(pool))
        return known.label if known else pool

    async def async_select_option(self, option: str) -> None:
        pool = self._by_label.get(option)
        if pool is None:
            # The live-pool option above is not a destination: it is already
            # where the rig is, and it may not even be a pool this entry knows
            # how to describe.
            raise HomeAssistantError(f"{option} is not one of this rig's pools")

        self._pending = pool
        self._pending_polls = 0
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_pool(pool.url)
        except Exception:
            self._pending = None
            self.async_write_ha_state()
            raise

    # A pool change is confirmed within a poll or two. Past that, the miner is
    # not going where it was sent -- a pool refusing the login, most likely --
    # and continuing to display the request as though it were the state would
    # be a lie that never expires.
    _PENDING_POLLS = 3

    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic value once the miner agrees with it, or gives up.

        A match clears it immediately. Clearing on the first poll instead would
        put the old pool back on screen for as long as the reconnect takes,
        which is the very flicker this exists to avoid.
        """
        if self._pending is not None:
            self._pending_polls += 1
            if (
                self._live == self._pending.label
                or self._pending_polls >= self._PENDING_POLLS
            ):
                self._pending = None
        super()._handle_coordinator_update()
