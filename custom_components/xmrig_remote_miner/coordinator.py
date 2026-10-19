"""Polling coordinator and HTTP client for one XMRig instance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_MAC,
    CONF_MQTT_DEVICE,
    CONF_WORKER_ID,
    CONF_POWER_CAPS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    POOL_SWITCH_SETTLE,
    MANUFACTURER,
)
from .pools import normalize as normalize_pool
from .mqtt_power import (
    MqttPower,
    PowerCapabilities,
    async_machine_state,
    async_probe,
    async_state_entity,
    async_temperature_entity,
)

_LOGGER = logging.getLogger(__name__)


# XMRig's user-agent string carries the OS in parentheses, first:
#   XMRig/6.24.0 (Linux x86_64) libuv/1.48.0 gcc/13.3.0
#   XMRig/6.24.0 (Windows NT 10.0; Win64; x64) libuv/1.48.0 msvc/2022
# Keep the first segment before ";", which is the part naming the OS; the rest
# describes the ABI and says nothing more.
_UA_OS = re.compile(r"\(([^)]*)\)")


def miner_os(data: dict) -> str | None:
    """The machine's OS, as XMRig describes itself.

    Taken from the user-agent string: it is already in the summary fetched on
    every poll, so a fact that never changes costs no request of its own. Less
    elegant, free.
    """
    match = _UA_OS.search(data.get("ua") or "")
    if not match:
        return None
    return match.group(1).split(";")[0].strip() or None


def miner_memory_total(data: dict) -> int | None:
    """Total RAM, in bytes.

    Makes the memory-usage sensor interpretable: a percentage alone does not say
    whether 400 MB or 12 GB is left. Free memory gets no sensor of its own for
    that -- with the total and the percentage, it follows.
    """
    memory = (data.get("resources") or {}).get("memory") or {}
    total = memory.get("total")
    return int(total) if total else None


async def async_request(
    host: str,
    port: int,
    token: str,
    method: str,
    path: str,
    body: str | None = None,
    *,
    what: str,
) -> tuple[int, bytes]:
    """One request to XMRig over a raw socket, returning the status and the body.

    The only HTTP client in this integration, for reads and commands alike. A
    raw HTTP/1.0 client rather than aiohttp: XMRig's server sometimes returns a
    duplicated response ("Data after Connection: close") which aiohttp's strict
    parser rejects even though the request was applied, and it closes the
    connection after every request anyway, so a connection pool would keep
    nothing but stale sockets. Here the response is read until close and
    interpreted the way curl would -- status line, then whatever came after
    the headers.

    `what` only names the operation in the error messages; it is the
    difference between "XMRig unreachable" and knowing which press caused it.
    """
    # An IPv6 literal may be stored bracketed, as it is written in a URL. The
    # socket wants it bare and the Host header wants it bracketed.
    bare = host.strip("[]")
    head = [
        f"{method} {path} HTTP/1.0",
        f"Host: {f'[{bare}]' if ':' in bare else bare}:{port}",
        f"Authorization: Bearer {token}",
        "Connection: close",
    ]
    if body is not None:
        head += [
            "Content-Type: application/json",
            f"Content-Length: {len(body.encode())}",
        ]
    request = ("\r\n".join(head) + "\r\n\r\n" + (body or "")).encode()

    try:
        async with asyncio.timeout(10):
            reader, writer = await asyncio.open_connection(bare, port)
            try:
                writer.write(request)
                await writer.drain()
                raw = await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
    # ValueError as well: a host name with an empty or over-long label fails in
    # the IDNA codec with a UnicodeError, before any socket exists.
    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(f"XMRig unreachable for {what}: {err}") from err

    status_line = raw.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ")
    try:
        status = int(parts[1])
    except (IndexError, ValueError) as err:
        raise HomeAssistantError(
            f"Unintelligible XMRig response for {what}:"
            f" {status_line.decode(errors='replace')}"
        ) from err

    # The headers end at the first blank line. A duplicated response leaves
    # a second one glued to the end of this body, which is why the callers
    # read the first JSON value out of it rather than the whole string.
    _, _, payload = raw.partition(b"\r\n\r\n")
    return status, payload


def first_json(payload: bytes) -> Any:
    """The first JSON value in a response body. Raises ValueError.

    raw_decode, not loads: a duplicated response would put a second copy of
    the JSON straight after the first, and taking the first value is exactly
    right in both cases.
    """
    value, _ = json.JSONDecoder().raw_decode(
        payload.decode(errors="replace").lstrip()
    )
    return value


class XmrigCoordinator(DataUpdateCoordinator[dict]):
    """Polls the XMRig summary and sends the control commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._token: str = entry.data[CONF_TOKEN]

        # What the last successful probe established, read back from the config
        # entry. This is what lets the buttons exist even while the machine is
        # off -- the wake button is only wanted at that moment, and a probe can
        # learn nothing from a machine that is not running. None means "nothing
        # was ever known", not "there is nothing to do".
        self.power_capabilities: PowerCapabilities | None = (
            PowerCapabilities.from_dict(entry.data.get(CONF_POWER_CAPS))
        )
        # Explicitly chosen MQTT device, or None for automatic matching on the
        # worker_id XMRig publishes.
        self.mqtt_device: str | None = entry.data.get(CONF_MQTT_DEVICE)
        # Hand-typed MAC, for a machine whose MQTT device does not declare one.
        self._configured_mac: str | None = entry.data.get(CONF_MAC)
        # The entities to press, once the probe has found them.
        self._power: MqttPower | None = None
        # Clamped, not trusted. The config flow enforces a minimum, but an
        # entry created before it did could hold 0 -- and an update_interval of
        # zero means the next refresh is always overdue, i.e. a loop that polls
        # the miner's HTTP API as fast as the event loop allows.
        interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        if not isinstance(interval, int) or interval < MIN_SCAN_INTERVAL:
            interval = MIN_SCAN_INTERVAL
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly: relying on the implicit current-entry
            # ContextVar is deprecated for custom integrations and is due to be
            # removed.
            config_entry=entry,
            name=f"XMRig {entry.title}",
            update_interval=timedelta(seconds=interval),
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def miner(self) -> dict:
        """The XMRig summary from the last poll."""
        return self.data or {}

    @property
    def worker_id(self) -> str | None:
        """The name the miner gives itself. Used to find its MQTT device.

        Falls back to the remembered one, because this is read at its most
        useful when the miner is not answering and there is no summary to read
        it from. Live first all the same: a machine renamed is a machine whose
        remembered name is now wrong.
        """
        return self.miner.get("worker_id") or self.entry.data.get(CONF_WORKER_ID)

    @property
    def machine_state(self) -> str | None:
        """What the machine says about itself when XMRig has stopped answering.

        The remembered MAC is passed as a last way in, because this is read
        precisely when the machine is off -- and a machine that was already off
        when this integration was updated never got to teach it its name.
        """
        return async_machine_state(self.hass, self.worker_id, *self._lookup)


    @property
    def machine_entity(self) -> str | None:
        """The entity the machine publishes its own state on, if it has one."""
        return async_state_entity(self.hass, self.worker_id, *self._lookup)

    @property
    def temperature_entity(self) -> str | None:
        """The CPU temperature the machine publishes, if it has one."""
        return async_temperature_entity(self.hass, self.worker_id, *self._lookup)

    @property
    def _lookup(self) -> tuple[str | None, str | None]:
        """The chosen device and the remembered MAC, in that order of authority."""
        caps = self.power_capabilities
        return (
            self.mqtt_device,
            self._configured_mac or (caps.mac if caps else None),
        )

    async def _async_update_data(self) -> dict:
        try:
            status, payload = await self._async_raw_request(
                "GET", "/1/summary", what="the summary poll"
            )
        except HomeAssistantError as err:
            raise UpdateFailed(str(err)) from err
        if status in (401, 403):
            raise UpdateFailed("access token refused (401/403)")
        if status != 200:
            raise UpdateFailed(f"XMRig answered the summary poll with HTTP {status}")
        try:
            summary = first_json(payload)
        except ValueError as err:
            raise UpdateFailed("XMRig's summary did not parse as JSON") from err
        if not isinstance(summary, dict):
            raise UpdateFailed("XMRig's summary was not an object")
        return summary

    def _probe_power(self) -> MqttPower | None:
        """Look up the machine's MQTT device, MAC fallbacks included.

        The MAC already remembered is the last resort, and that ordering is what
        keeps an established wake button alive. Some entries carry a MAC that
        nothing publishes any more -- a Windows host, whose HASS.Agent device
        declares no connections and whose MAC was resolved once by other means.
        Letting a probe return None for it would quietly overwrite a working
        button with nothing, and only while the machine was off would anyone
        find out.
        """
        known = self.power_capabilities
        return async_probe(
            self.hass,
            self.worker_id,
            self.mqtt_device,
            self._configured_mac or (known.mac if known else None),
        )

    async def async_probe_power(self) -> None:
        """Ask the machine's MQTT device once what it permits.

        An unsuccessful probe never erases what was already known. A device can
        be missing for reasons that have nothing to do with the machine -- the
        MQTT integration still starting, a retained discovery message not
        replayed yet -- and taking that silence for "this machine has no wake
        button" would remove the button exactly when it becomes useful. Only a
        positive result writes.
        """
        power = self._probe_power()
        if power is None:
            return

        self._power = power
        self.power_capabilities = self._keep_known_actions(power.capabilities)
        self._persist_power_capabilities(self.power_capabilities)

    def _keep_known_actions(self, found: PowerCapabilities) -> PowerCapabilities:
        """Never let a probe that found no command at all erase the ones known.

        A probe that finds the device and its MAC but none of its commands
        counts as successful, because a wake button alone is still something.
        Taken at face value it would write `actions: []` straight over `off,
        reboot, suspend`, and the rig would lose its power buttons at the next
        reload.

        A machine that publishes a device but no command is a real case, so
        this cannot be an error. It is simply never new information: a rig that
        could be shut down yesterday still has the hardware for it today, and
        the ways of reading zero commands -- discovery not replayed yet, the
        entities disabled, MQTT still connecting -- are all conditions of this
        Home Assistant rather than facts about the machine.

        So zero commands is treated as "learned nothing", and what was already
        known stands. A capability genuinely taken away lingers as a button
        that says the machine does not expose that action, which is a legible
        failure; the alternative was a wake button silently disappearing while
        the rig was off.
        """
        known = self.power_capabilities
        if found.actions or known is None or not known.actions:
            return found

        _LOGGER.debug(
            "Probe of %s found no command entity; keeping the known %s",
            self.host,
            sorted(known.actions),
        )
        return PowerCapabilities(
            actions=known.actions, mac=found.mac or known.mac
        )

    def _persist_power_capabilities(self, caps: PowerCapabilities) -> None:
        """Store the capabilities and the worker name in the entry, if either changed.

        Conditional so as not to rewrite `.storage` on every start-up for
        identical content. The integration deliberately installs no update
        listener (see __init__.py), so this write reloads nothing: it happens
        during setup, and a reload at that moment would loop.

        The worker name rides along because it is remembered for the same
        reason and learned at the same moment -- from a machine that is
        answering -- and is wanted at the same one, when it is not.
        """
        data = dict(self.entry.data)
        stored = caps.as_dict()
        if stored != data.get(CONF_POWER_CAPS):
            _LOGGER.debug("Power capabilities of %s remembered: %s", self.host, stored)
            data[CONF_POWER_CAPS] = stored
        # Only from a live summary. `self.worker_id` would happily write back
        # what it just read out of the entry, which is not learning anything.
        if (live := self.miner.get("worker_id")) and live != data.get(CONF_WORKER_ID):
            data[CONF_WORKER_ID] = live
        if data == self.entry.data:
            return
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    async def async_power_command(self, action: str) -> None:
        """Power the machine off, reboot it or suspend it.

        There is nothing to catch here: the command goes out over MQTT and the
        broker acknowledges it long before the machine starts acting on it. The
        machine executing its own shutdown is what makes that true -- no session
        is being cut from under us.

        The probe is retried when nothing was found at start-up. The buttons are
        built from the capabilities remembered in the config entry, so they
        exist while the machine is off; the entities to press are looked up
        live, and at start-up they may simply not have been there yet.
        """
        if self._power is None:
            self._power = self._probe_power()
        if self._power is None:
            raise HomeAssistantError(
                f"no MQTT device found right now for {self.host}"
            )

        await self._power.async_press(self.hass, action)

    async def _async_raw_request(
        self, method: str, path: str, body: str | None = None, *, what: str
    ) -> tuple[int, bytes]:
        return await async_request(
            self.host, self.port, self._token, method, path, body, what=what
        )

    async def async_send_command(self, method: str) -> None:
        """Send pause/resume/stop through the json_rpc API, then refresh state."""
        status, payload = await self._async_raw_request(
            "POST",
            "/json_rpc",
            json.dumps({"method": method, "id": 1}),
            what=f"command {method}",
        )
        if status != 200:
            raise HomeAssistantError(f"XMRig refused command {method}: HTTP {status}")
        if b'"status"' not in payload or b"OK" not in payload:
            raise HomeAssistantError(
                f"Unexpected XMRig response for command {method}"
            )

        await self.async_request_refresh()

    # --- Pool selection -------------------------------------------------------

    @property
    def pool(self) -> str | None:
        """The pool the miner is connected to, as it reports it."""
        return (self.miner.get("connection") or {}).get("pool") or None

    async def async_get_config(self) -> dict:
        """The miner's live config, as XMRig would write it to disk.

        Only reachable when XMRig runs with `http.restricted = false`, which is
        also what pause and resume need -- so a rig this integration can already
        control can be repointed too, and one that refuses this refuses those.
        """
        status, payload = await self._async_raw_request(
            "GET", "/2/config", what="reading the config"
        )
        if status in (401, 403):
            raise HomeAssistantError(
                "XMRig refused the access token when reading its config"
            )
        if status == 404:
            raise HomeAssistantError(
                "This XMRig does not serve /2/config — it is running with"
                " http.restricted enabled, so its pool cannot be changed"
                " remotely."
            )
        if status != 200:
            raise HomeAssistantError(f"XMRig refused to hand over its config: HTTP {status}")

        try:
            config = first_json(payload)
        except ValueError as err:
            raise HomeAssistantError("XMRig's config did not parse as JSON") from err

        if not isinstance(config, dict):
            raise HomeAssistantError("XMRig's config was not an object")
        return config

    async def async_set_pool(self, url: str) -> None:
        """Point the miner at `url`, keeping everything else it was told.

        The config is read back and edited rather than composed here, because
        it holds things this integration has no business inventing -- the
        wallet, the worker name, the TLS flag, the access token itself. Only
        the first pool's address changes; any further pools, which would be
        failover targets, are left where they are.

        XMRig applies this by reloading its config, which drops the stratum
        connection and redials. The RandomX dataset is not rebuilt for a move
        between pools on the same chain, so the cost is the reconnect, not a
        restart.
        """
        config = await self.async_get_config()

        pools = config.get("pools")
        if not isinstance(pools, list) or not pools or not isinstance(pools[0], dict):
            raise HomeAssistantError(
                "XMRig reports no configured pool to replace"
            )

        primary = dict(pools[0])
        if normalize_pool(primary.get("url") or "") == normalize_pool(url):
            return
        primary["url"] = url
        config["pools"] = [primary, *pools[1:]]

        status, _ = await self._async_raw_request(
            "PUT", "/2/config", json.dumps(config), what="changing the pool"
        )
        if status not in range(200, 300):
            raise HomeAssistantError(
                f"XMRig refused the new pool: HTTP {status}"
            )

        _LOGGER.debug("%s repointed at %s", self.host, url)
        # The miner needs a moment to drop the old connection and dial the new
        # one; polling immediately would read the pool it has just left. The
        # select shows the requested pool in the meantime, so this is only
        # about how soon the sensors agree with it.
        await asyncio.sleep(POOL_SWITCH_SETTLE)
        await self.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        """The "Device info" card on the rig's page.

        The OS and the RAM appear there in addition to their sensors. That is
        not pointless duplication: this card does not display entities, only the
        fields below, and it is where one looks to find out *what* a machine is.
        The sensors, meanwhile, keep the history and serve automations. Two
        uses, two places.

        For want of a "system" field in Home Assistant, the OS is appended to
        the XMRig version -- both are software -- and the RAM takes the hardware
        version slot.
        """
        data = self.miner
        cpu = data.get("cpu") or {}

        version = data.get("version")
        os_name = miner_os(data)
        if version and os_name:
            sw_version = f"XMRig {version} ({os_name})"
        elif version:
            sw_version = f"XMRig {version}"
        else:
            sw_version = os_name

        total = miner_memory_total(data)
        hw_version = f"{total / 1024 ** 3:.1f} GiB RAM" if total else None

        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            manufacturer=MANUFACTURER,
            model=cpu.get("brand"),
            sw_version=sw_version,
            hw_version=hw_version,
            configuration_url=self.base_url,
        )
