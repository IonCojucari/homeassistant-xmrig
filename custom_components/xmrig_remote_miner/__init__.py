"""XMRig integration: monitoring and control of mining instances.

One config entry = one rig, registered as a single device carrying all of its
sensors and its run/pause switch.
"""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .coordinator import XmrigCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

type XmrigConfigEntry = ConfigEntry[XmrigCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: XmrigConfigEntry) -> bool:
    """Set up one XMRig rig.

    Two possible start-ups, and the difference between them is the wake button.

    With no remembered power capabilities, this behaves like any other local
    integration: if the miner does not answer, raise ConfigEntryNotReady and let
    Home Assistant retry. That is the right reflex for a rig that has just been
    added, where there is nothing to control anyway.

    With remembered capabilities, no. That path exists precisely for the machine
    that is switched off: it does not answer, and that is exactly the moment one
    wants to press "wake". Refusing to load the entry would remove the button at
    that very moment. So the entry loads degraded instead: the buttons exist, the
    sensors are unavailable, and the coordinator recovers on its own as soon as
    the miner answers.
    """
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True))
    coordinator = XmrigCoordinator(hass, entry, session)

    try:
        if coordinator.power_capabilities is None:
            await coordinator.async_config_entry_first_refresh()
        else:
            # Does not raise: only sets last_update_success.
            await coordinator.async_refresh()

        # Probing needs a machine that is switched on, so it only makes sense
        # when the miner has just answered. The result is stored in the config
        # entry, which is what makes the degraded start-up above possible next
        # time. Never raises: a machine that exposes nothing simply yields a rig
        # with no buttons.
        if coordinator.last_update_success:
            await coordinator.async_probe_power()

        # Inside the try as well: a platform raising here fails the setup and
        # Home Assistant retries, and every attempt that left this out stranded
        # a live ClientSession and its connector -- one per retry, each with an
        # "Unclosed client session" warning behind it.
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await session.close()
        raise

    if not coordinator.last_update_success:
        _async_reload_when_miner_returns(hass, entry, coordinator)

    # No update listener here, deliberately. The Reconfigure flow reloads by
    # itself (async_update_reload_and_abort), and the entry is also written to
    # remember the power capabilities -- a write that must not trigger a reload,
    # or setup would loop on itself.
    return True


@callback
def _async_reload_when_miner_returns(
    hass: HomeAssistant, entry: XmrigConfigEntry, coordinator: XmrigCoordinator
) -> None:
    """Reload the entry on the first successful poll after a degraded start-up.

    Platforms decide *at setup time* which entities exist: the Glances sensors
    are only created if Glances answered, and the buttons only from the known
    capabilities. Starting against a machine that is off therefore freezes an
    incomplete list, which would stay that way until the next Home Assistant
    restart. Reloading as soon as the miner answers rebuilds it.

    The reload unloads the entry, which removes this listener; the flag only
    prevents scheduling two reloads if several polls succeed in the same window.
    """
    reloading = False

    @callback
    def _check() -> None:
        nonlocal reloading
        if reloading or not coordinator.last_update_success:
            return
        reloading = True
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

    entry.async_on_unload(coordinator.async_add_listener(_check))


async def async_unload_entry(hass: HomeAssistant, entry: XmrigConfigEntry) -> bool:
    """Unload a rig and close its HTTP session."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.session.close()
    return unload_ok
