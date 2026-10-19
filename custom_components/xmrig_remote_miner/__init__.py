"""XMRig integration: monitoring and control of mining instances.

One config entry = one rig, registered as a single device carrying all of its
sensors and its run/pause switch.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CAPS_ACTIONS, CAPS_MAC, CONF_MQTT_DEVICE, CONF_POWER_CAPS, DOMAIN
from .coordinator import XmrigCoordinator

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type XmrigConfigEntry = ConfigEntry[XmrigCoordinator]


# Keys that older entries carry and nothing reads: the SSH power control of
# version 1, and the Glances telemetry of version 2.
_OBSOLETE_KEYS = {
    1: ("ssh_user", "ssh_port", "ssh_key"),
    2: ("glances_port", "glances_user", "glances_password"),
}


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop the settings of mechanisms this integration no longer has.

    Nothing would break if they stayed -- they are simply never read -- but a
    stale SSH key path or Glances password sitting in a config entry is the
    kind of thing someone finds in two years and believes. So they go,
    explicitly and once, rather than lingering.

    From version 1, the device field is also renamed rather than dropped: it
    still points at the same MQTT device, it just does not belong only to
    HASS.Agent.
    """
    if entry.version >= 3:
        return True

    data = dict(entry.data)
    for version, keys in _OBSOLETE_KEYS.items():
        if entry.version <= version:
            for key in keys:
                data.pop(key, None)

    if entry.version == 1:
        if (device := data.pop("hass_agent_device", None)) is not None:
            data[CONF_MQTT_DEVICE] = device
        if isinstance(caps := data.get(CONF_POWER_CAPS), dict):
            data[CONF_POWER_CAPS] = {
                k: v for k, v in caps.items() if k in (CAPS_ACTIONS, CAPS_MAC)
            }

    # Glances' CPU usage has no source left, so its entity would linger as "no
    # longer provided" forever. Its CPU temperature is not removed: the same
    # unique_id is reused by the mirrored temperature, history included.
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_cpu_usage"
    ):
        registry.async_remove(entity_id)

    hass.config_entries.async_update_entry(entry, data=data, version=3)
    return True


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
    coordinator = XmrigCoordinator(hass, entry)

    if coordinator.power_capabilities is None:
        await coordinator.async_config_entry_first_refresh()
    else:
        # Does not raise: only sets last_update_success.
        await coordinator.async_refresh()

    # Probing looks for the machine's MQTT device, so it only makes sense once
    # the miner has answered and named itself. The result is stored in the
    # config entry, which is what makes the degraded start-up above possible
    # next time. Never raises: a machine that exposes nothing simply yields a
    # rig with no buttons.
    if coordinator.last_update_success:
        await coordinator.async_probe_power()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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

    Setup decides once what it can only learn from a running machine: the
    buttons come from the capabilities probed at that moment, and the state
    sensor subscribes to the machine's state entity if one was found. Starting
    against a machine that is off therefore freezes an incomplete picture,
    which would stay that way until the next Home Assistant restart. Reloading
    as soon as the miner answers rebuilds it.

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
    """Unload a rig."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
