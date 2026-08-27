"""Polling coordinator and HTTP client for one XMRig instance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta

import aiohttp

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
    CONF_GLANCES_PASSWORD,
    CONF_GLANCES_PORT,
    CONF_GLANCES_USER,
    CONF_MAC,
    CONF_MQTT_DEVICE,
    CONF_WORKER_ID,
    MACHINE_READY,
    CONF_POWER_CAPS,
    DEFAULT_GLANCES_PORT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    GLANCES_API_VERSIONS,
    GLANCES_PLUGINS,
    MANUFACTURER,
)
from .mqtt_power import (
    MqttPower,
    PowerCapabilities,
    async_machine_raw,
    async_machine_state,
    async_probe,
    async_state_entity,
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

    Taken from the user-agent string rather than from Glances' `system` plugin:
    it is already in the summary fetched on every poll, whereas Glances would
    cost one more HTTP request each time for a fact that never changes. Less
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


class XmrigCoordinator(DataUpdateCoordinator[dict]):
    """Polls the XMRig summary and sends the control commands.

    Also polls, if it answers, a Glances instance on the same machine for
    temperature and load -- numbers XMRig does not report and that explain half
    the hashrate variation. That second source is strictly optional: it cannot
    fail a poll, and its sensors are not even created if Glances does not answer
    at start-up.

    A dedicated aiohttp session with ``force_close=True`` is used: XMRig answers
    "Connection: close" on every request, which stales the connections a shared
    pool keeps and causes intermittent ClientErrors on the command POST. Forcing
    a fresh connection per call makes the problem disappear for both reads and
    writes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
    ) -> None:
        self.entry = entry
        self.session = session
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        self._token: str = entry.data[CONF_TOKEN]
        self.glances_port: int = entry.data.get(
            CONF_GLANCES_PORT, DEFAULT_GLANCES_PORT
        )
        # Glances HTTP Basic credentials, when it asks for them. Absent for an
        # unprotected instance, which is still Glances' default.
        glances_user = entry.data.get(CONF_GLANCES_USER)
        self._glances_auth: aiohttp.BasicAuth | None = (
            aiohttp.BasicAuth(
                glances_user, entry.data.get(CONF_GLANCES_PASSWORD) or ""
            )
            if glances_user
            else None
        )
        # The Glances API version settled on at first contact, or None while
        # none has been reached successfully yet.
        self._glances_api: int | None = None
        self._glances_auth_warned = False

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
        # Clamped, not trusted. The config flow now enforces a minimum, but an
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
        return (self.data or {}).get("miner") or {}

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
    def machine_ready(self) -> bool | None:
        """Whether the machine says it has finished starting. None if it does not say.

        Authoritative where it exists: the agent watches the miner from the same
        box, every few seconds, and does not depend on this poll succeeding.
        """
        raw = async_machine_raw(self.hass, self.worker_id, *self._lookup)
        return None if raw is None else raw == MACHINE_READY

    @property
    def machine_entity(self) -> str | None:
        """The entity the machine publishes its own state on, if it has one."""
        return async_state_entity(self.hass, self.worker_id, *self._lookup)

    @property
    def _lookup(self) -> tuple[str | None, str | None]:
        """The chosen device and the remembered MAC, in that order of authority."""
        caps = self.power_capabilities
        return (
            self.mqtt_device,
            self._configured_mac or (caps.mac if caps else None),
        )

    @property
    def glances(self) -> dict | None:
        """Glances telemetry, or None if unavailable/disabled."""
        return (self.data or {}).get("glances")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _async_update_data(self) -> dict:
        """Poll the miner, then the system telemetry if it is there.

        Only XMRig can fail the poll. Glances is a bonus: a telemetry outage
        must not take the hashrate and the shares down with it, or the
        measurement is lost at exactly the moment it is needed.
        """
        miner = await self._async_fetch_miner()
        return {"miner": miner, "glances": await self._async_fetch_glances()}

    async def _async_fetch_miner(self) -> dict:
        try:
            async with asyncio.timeout(10):
                async with self.session.get(
                    f"{self.base_url}/1/summary", headers=self._headers
                ) as resp:
                    if resp.status in (401, 403):
                        raise UpdateFailed("access token refused (401/403)")
                    resp.raise_for_status()
                    return await resp.json()
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise UpdateFailed("timed out polling XMRig") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"connection error talking to XMRig: {err}") from err

    async def _async_fetch_glances(self) -> dict | None:
        """Return {plugin: payload}, or None if Glances does not answer."""
        if not self.glances_port:
            return None

        versions = (
            (self._glances_api,) if self._glances_api else GLANCES_API_VERSIONS
        )
        for version in versions:
            base = f"http://{self.host}:{self.glances_port}/api/{version}"
            try:
                async with asyncio.timeout(10):
                    # return_exceptions, so that one plugin failing does not
                    # abandon the other three still in flight: gather would
                    # propagate immediately and leave them running outside this
                    # timeout with nobody collecting their results, which shows
                    # up as "Task exception was never retrieved". The normal way
                    # to hit that is a Glances 3 host being asked for /api/4.
                    results = await asyncio.gather(
                        *(self._async_get_json(f"{base}/{p}") for p in GLANCES_PLUGINS),
                        return_exceptions=True,
                    )
            except (TimeoutError, asyncio.TimeoutError):
                continue

            # One failure disqualifies this API version, with one exception
            # worth telling apart: a 401/403 is not "Glances is absent" but
            # "Glances is there and refuses these credentials". Without that
            # distinction, a protected instance with the wrong password looks
            # exactly like a machine with no Glances -- four sensors that never
            # exist, and nothing to say why.
            failure = next(
                (r for r in results if isinstance(r, BaseException)), None
            )
            if failure is not None:
                if (
                    isinstance(failure, aiohttp.ClientResponseError)
                    and failure.status in (401, 403)
                ):
                    # Warn once. The poll runs every 20 seconds and a wrong
                    # password stays wrong until someone fixes it; repeating
                    # would teach nothing and drown the log.
                    if not self._glances_auth_warned:
                        self._glances_auth_warned = True
                        _LOGGER.warning(
                            "Glances on %s:%s refuses the credentials (%s) —"
                            " system sensors skipped. Fix them with"
                            " Reconfigure on this rig.",
                            self.host,
                            self.glances_port,
                            failure.status,
                        )
                    return None
                continue

            self._glances_api = version
            return dict(zip(GLANCES_PLUGINS, results, strict=True))

        # Unlatch the remembered version. Without this, a rig upgraded from
        # Glances 3 to 4 would keep being asked for /api/3 forever: the loop
        # above narrows to the remembered version once one has worked, so a
        # permanent 404 there would take the system sensors down until the
        # entry was reloaded.
        self._glances_api = None
        _LOGGER.debug(
            "Glances unreachable on %s:%s, system sensors skipped",
            self.host,
            self.glances_port,
        )
        return None

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
        self.power_capabilities = power.capabilities
        self._persist_power_capabilities(power.capabilities)

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

    async def _async_get_json(self, url: str) -> dict | list:
        async with self.session.get(url, auth=self._glances_auth) as resp:
            resp.raise_for_status()
            # Glances serves JSON as text/html on some versions, hence
            # content_type=None rather than raising on the MIME type.
            return await resp.json(content_type=None)

    async def async_send_command(self, method: str) -> None:
        """Send pause/resume/stop through the json_rpc API, then refresh state.

        A raw HTTP/1.0 client rather than aiohttp: XMRig's server sometimes
        returns a duplicated response ("Data after Connection: close") which
        aiohttp's strict parser rejects even though the command was applied.
        Here the response is read until close and only the 200 status plus an
        "OK" body are checked, the way curl would.
        """
        body = json.dumps({"method": method, "id": 1})
        request = (
            f"POST /json_rpc HTTP/1.0\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Authorization: Bearer {self._token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        ).encode()

        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    writer.write(request)
                    await writer.drain()
                    raw = await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"XMRig unreachable for command {method}: {err}"
            ) from err

        status_line = raw.split(b"\r\n", 1)[0]
        if b" 200 " not in status_line and not status_line.rstrip().endswith(b"200 OK"):
            raise HomeAssistantError(
                f"XMRig refused command {method}: {status_line.decode(errors='replace')}"
            )
        if b'"status"' not in raw or b"OK" not in raw:
            raise HomeAssistantError(
                f"Unexpected XMRig response for command {method}"
            )

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
