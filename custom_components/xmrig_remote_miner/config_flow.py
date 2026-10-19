"""Config flow UI for the XMRig integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_MAC,
    CONF_MQTT_DEVICE,
    CONF_POOLS,
    CONF_POWER_CAPS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import async_request
from .pools import parse_line

# Fields to mask on entry and on display. These are secrets, and the form gets
# filled in in the middle of a living room.
_SECRET = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# Bounds, so that a slip in the form cannot produce an entry that is accepted
# and then misbehaves: a scan interval of 0 makes the coordinator's next
# refresh permanently overdue, i.e. a loop polling the miner as fast as the
# event loop allows.
_PORT = vol.All(int, vol.Range(min=1, max=65535))

# The pool list is free text, so it gets the box that looks like free text.
_MULTILINE = selector.TextSelector(
    selector.TextSelectorConfig(multiline=True)
)


def _pool_error(raw: str | None) -> str | None:
    """Refuse a pool list that would silently lose lines, and only that.

    pools.parse drops what it cannot read, because it runs long after the form
    is gone and has nobody to tell. Here there is somebody to tell, so the same
    lines are read again strictly: a line has to yield a pool, and a pool needs
    a port -- an address without one is the mistake that produces a rig that
    quietly never connects.
    """
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        pool = parse_line(line)
        # `key`, not `url`: the scheme is already stripped there, so a
        # "stratum+ssl://" prefix does not count as the port's colon.
        if pool is None or ":" not in pool.key:
            return "invalid_pools"
        if pool.key in seen:
            return "duplicate_pools"
        seen.add(pool.key)
    return None


async def _async_validate(data: dict[str, Any]) -> str | None:
    """Return None if the connection is good, otherwise an error code."""
    try:
        status, _ = await async_request(
            data[CONF_HOST],
            data.get(CONF_PORT, DEFAULT_PORT),
            data[CONF_TOKEN],
            "GET",
            "/1/summary",
            what="the connection test",
        )
    except HomeAssistantError:
        return "cannot_connect"
    if status in (401, 403):
        return "invalid_auth"
    return None if status == 200 else "cannot_connect"


def _schema(defaults: dict[str, Any], *, with_name: bool) -> vol.Schema:
    """The rig's form. Shared between adding and reconfiguring.

    `defaults` pre-fills the fields: empty when adding, the existing entry when
    reconfiguring. One schema for both, so that a field added to the add flow
    has no chance of being missing from the place you correct it.

    The name only appears when adding: it becomes the entry's title, which Home
    Assistant already lets you rename from the device page.
    """
    fields: dict[Any, Any] = {}

    if with_name:
        # With a default: without one, a failed validation re-renders the form
        # with the name blank and still required, while every other field keeps
        # what was typed.
        fields[vol.Required(
            CONF_NAME, default=defaults.get(CONF_NAME, vol.UNDEFINED)
        )] = str

    fields.update(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)
            ): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): _PORT,
            vol.Required(
                CONF_TOKEN, default=defaults.get(CONF_TOKEN, vol.UNDEFINED)
            ): _SECRET,
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL)),
            # Only for a machine whose MQTT device does not declare its own
            # MAC, which in practice means a Windows host running HASS.Agent.
            # Not validated in the form: a malformed value is ignored later and
            # costs a wake button, whereas refusing the whole rig over it would
            # be out of proportion.
            vol.Optional(CONF_MAC, default=defaults.get(CONF_MAC, "")): str,
            # One pool per line, "Label = host:port" or just "host:port".
            # Fewer than two lines means no choice to make, and no select
            # entity is created -- see select.py.
            vol.Optional(
                CONF_POOLS, default=defaults.get(CONF_POOLS, "")
            ): _MULTILINE,
        }
    )

    # Leaving this empty is enough in the common case: the device is found from
    # the worker_id XMRig publishes, which is the machine name on both sides.
    # The field is only for when the two names have drifted apart.
    # No `default`: a DeviceSelector does not accept the empty string.
    device = defaults.get(CONF_MQTT_DEVICE)
    key = (
        vol.Optional(CONF_MQTT_DEVICE, default=device)
        if device
        else vol.Optional(CONF_MQTT_DEVICE)
    )
    fields[key] = selector.DeviceSelector(
        selector.DeviceSelectorConfig(integration="mqtt")
    )

    return vol.Schema(fields)


class XmrigConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adding and reconfiguring an XMRig rig through the UI."""

    # 2 dropped the SSH fields, 3 the Glances ones; async_migrate_entry cleans
    # the older entries.
    VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = _pool_error(user_input.get(CONF_POOLS)) or await _async_validate(
                user_input
            )
            if error is None:
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}, with_name=True),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Correct a rig that is already added, without deleting it.

        Deleting and recreating the entry would change its `entry_id`, and the
        entities' `unique_id`s derive from that -- so every entity would be
        renamed and all long-term history lost, over one edited token.

        The entry's own `unique_id` is deliberately not touched here. It only
        ever served to stop the same rig being added twice; making it follow an
        address change would mostly add new ways to collide with another entry.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = _pool_error(user_input.get(CONF_POOLS)) or await _async_validate(
                user_input
            )
            if error is None:
                # The remembered power capabilities are preserved: this form
                # says nothing about them, and losing them would make the wake
                # button disappear until the next successful probe -- that is,
                # until the machine is switched back on, which is exactly what
                # this is meant to avoid.
                updates = dict(user_input)
                if CONF_POWER_CAPS in entry.data:
                    updates[CONF_POWER_CAPS] = entry.data[CONF_POWER_CAPS]
                return self.async_update_reload_and_abort(entry, data=updates)
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or dict(entry.data), with_name=False),
            errors=errors,
        )
