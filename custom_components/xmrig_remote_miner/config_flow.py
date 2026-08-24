"""Config flow UI for the XMRig integration."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
)
from homeassistant.helpers import selector

from .const import (
    CONF_GLANCES_PASSWORD,
    CONF_GLANCES_PORT,
    CONF_GLANCES_USER,
    CONF_HASS_AGENT_DEVICE,
    CONF_POWER_CAPS,
    CONF_SSH_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USER,
    DEFAULT_GLANCES_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

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


async def _async_validate(data: dict[str, Any]) -> str | None:
    """Return None if the connection is good, otherwise an error code."""
    host = data[CONF_HOST]
    port = data.get(CONF_PORT, DEFAULT_PORT)
    url = f"http://{host}:{port}/1/summary"
    headers = {"Authorization": f"Bearer {data[CONF_TOKEN]}"}
    try:
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as resp:
                    if resp.status in (401, 403):
                        return "invalid_auth"
                    if resp.status != 200:
                        return "cannot_connect"
    except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError):
        return "cannot_connect"
    return None


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
            # Not validated: if Glances does not answer, the system sensors
            # are simply not created. Refusing to add the rig because an
            # optional source is missing would be disproportionate.
            # min=0 rather than 1: 0 is the documented "no Glances" value.
            vol.Optional(
                CONF_GLANCES_PORT,
                default=defaults.get(CONF_GLANCES_PORT, DEFAULT_GLANCES_PORT),
            ): vol.All(int, vol.Range(min=0, max=65535)),
            # Glances is unauthenticated out of the box, but can run behind
            # HTTP Basic -- which is how the NixOS rigs serve it, since its API
            # exposes the process list and the logged-in users. Leaving both
            # fields blank sends no credential.
            vol.Optional(
                CONF_GLANCES_USER, default=defaults.get(CONF_GLANCES_USER, "")
            ): str,
            vol.Optional(
                CONF_GLANCES_PASSWORD,
                default=defaults.get(CONF_GLANCES_PASSWORD, ""),
            ): _SECRET,
            # Not validated either: the machine is probed at start-up and the
            # power buttons only appear if it answers. A missing key, or a host
            # without rig-power, yields a rig with no buttons -- not a rig that
            # is refused.
            vol.Optional(
                CONF_SSH_USER, default=defaults.get(CONF_SSH_USER, DEFAULT_SSH_USER)
            ): str,
            vol.Optional(
                CONF_SSH_PORT, default=defaults.get(CONF_SSH_PORT, DEFAULT_SSH_PORT)
            ): _PORT,
            vol.Optional(
                CONF_SSH_KEY, default=defaults.get(CONF_SSH_KEY, DEFAULT_SSH_KEY)
            ): str,
        }
    )

    # The second resort, for machines that cannot answer over SSH -- typically
    # Windows. Leaving it empty is enough in the common case: the device is
    # found from the worker_id XMRig publishes, which is the machine name on
    # both sides. This field is only for when the two names have drifted apart.
    # No `default`: a DeviceSelector does not accept the empty string.
    agent = defaults.get(CONF_HASS_AGENT_DEVICE)
    key = (
        vol.Optional(CONF_HASS_AGENT_DEVICE, default=agent)
        if agent
        else vol.Optional(CONF_HASS_AGENT_DEVICE)
    )
    fields[key] = selector.DeviceSelector(
        selector.DeviceSelectorConfig(integration="mqtt")
    )

    return vol.Schema(fields)


class XmrigConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adding and reconfiguring an XMRig rig through the UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _async_validate(user_input)
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

        There used to be no way to do this: changing a token, a Glances port or
        a credential meant deleting the entry and recreating it, which changes
        its `entry_id`. The entities' `unique_id`s derive from that -- so every
        entity would have been renamed and all long-term history lost, over one
        edited password.

        The entry's own `unique_id` is deliberately not touched here. It only
        ever served to stop the same rig being added twice; making it follow an
        address change would mostly add new ways to collide with another entry.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await _async_validate(user_input)
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
