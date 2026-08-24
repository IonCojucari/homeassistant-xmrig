"""Polling coordinator and HTTP client for one XMRig instance."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
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
    CONFIG_RELOAD_DELAY,
    CONF_GLANCES_PASSWORD,
    CONF_GLANCES_PORT,
    CONF_GLANCES_USER,
    CONF_HASS_AGENT_DEVICE,
    CONF_POWER_CAPS,
    CONF_SSH_KEY,
    CONF_SSH_PORT,
    CONF_SSH_USER,
    DEFAULT_GLANCES_PORT,
    DEFAULT_SSH_KEY,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    GLANCES_API_VERSIONS,
    GLANCES_PLUGINS,
    MANUFACTURER,
    RIG_POWER_SUDO,
    RX_SCRATCHPAD_BYTES,
)
from .hass_agent import HassAgentPower
from .hass_agent import async_probe as async_probe_hass_agent
from .ssh import (
    DISCONNECTED_ERRORS,
    PowerCapabilities,
    SshRunner,
    resolve_mac_via_arp,
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


def hint_for_threads(threads: int, logical: int) -> int:
    """The `max-threads-hint` percentage that yields `threads` mining threads.

    XMRig computes round(logical * hint / 100), so this is that inverted. The
    rounding is why 3 threads out of 8 can be asked for as either 37% or 38% --
    both land on 3 -- and the clamp is because the ends are not free: 0% is
    refused by XMRig, and 100% is already its maximum.

    Exact on a CPU with a single L3, which is most of them. On a part with
    several top-level caches (Threadripper, Epyc, multi-CCX Ryzen) XMRig splits
    the budget per cache and drops the remainder, so the count that comes back
    can be lower than the one asked for. Nothing breaks: the entity reports what
    the miner actually runs, so the number simply lands short and can be nudged.
    """
    return max(1, min(100, round(threads * 100 / logical)))


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

        self.ssh = SshRunner(
            host=self.host,
            port=entry.data.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
            user=entry.data.get(CONF_SSH_USER, DEFAULT_SSH_USER),
            key_path=entry.data.get(CONF_SSH_KEY, DEFAULT_SSH_KEY),
        )
        # What the last successful probe established, read back from the config
        # entry. This is what lets the buttons exist even while the machine is
        # off -- the wake button is only wanted at that moment, and a probe can
        # learn nothing from a machine that is not running. None means "nothing
        # was ever known", not "there is nothing to do".
        self.power_capabilities: PowerCapabilities | None = (
            PowerCapabilities.from_dict(entry.data.get(CONF_POWER_CAPS))
        )
        # Explicitly chosen HASS.Agent device, or None for automatic matching
        # on the worker_id XMRig publishes.
        self.hass_agent_device: str | None = entry.data.get(CONF_HASS_AGENT_DEVICE)
        # Set only when HASS.Agent is what provides power control; None means
        # "SSH", including when there is nothing at all.
        self._hass_agent: HassAgentPower | None = None
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
        """The name the miner gives itself. Used to find the HASS.Agent device."""
        return self.miner.get("worker_id") or None

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
        return {
            "miner": miner,
            "cpu_backend": await self._async_fetch_cpu_backend(),
            "glances": await self._async_fetch_glances(),
        }

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

    async def _async_fetch_cpu_backend(self) -> dict | None:
        """The CPU backend, as /2/backends describes it, or None.

        Fetched for one number the summary cannot give honestly: its
        `hashrate.threads` is the concatenation of *every* enabled backend, so
        on a rig that also mines on a GPU it would count GPU threads as CPU
        ones -- and the thread control would show, and try to set, a number that
        means nothing.

        Like Glances, this can never fail a poll. It is a plain GET, so it works
        even against a restricted API, but a miner that does not serve it must
        not take the hashrate down with it: `mining_threads` falls back to the
        summary.
        """
        try:
            async with asyncio.timeout(10):
                async with self.session.get(
                    f"{self.base_url}/2/backends", headers=self._headers
                ) as resp:
                    resp.raise_for_status()
                    backends = await resp.json(content_type=None)
        except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError, ValueError):
            return None

        if not isinstance(backends, list):
            return None
        return next(
            (b for b in backends if isinstance(b, dict) and b.get("type") == "cpu"),
            None,
        )

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

    async def async_probe_power(self) -> None:
        """Ask the machine once what it permits.

        Two sources, in this strict order:

        1. SSH and `rig-power status`, the nominal path. A NixOS rig answers
           here and never sees the rest.
        2. HASS.Agent, for machines with no way to answer the first -- a Windows
           host, in practice.

        Then, only if a source has already established capabilities but without
        a MAC, an ARP fallback for waking. That condition is not cosmetic:
        applying the fallback to a machine whose probe returned nothing would
        make a wake button appear on rigs that had none, merely because they
        answer a ping. The fallback can therefore only complete, never create.

        An unsuccessful probe never erases what was already known: a machine
        that is off answers neither SSH nor ARP, and taking that silence for
        "this machine has no wake button" would remove the button exactly when
        it becomes useful. Only a positive result writes.
        """
        caps = await self.ssh.async_probe()

        if caps is None:
            agent = await async_probe_hass_agent(
                self.hass, self.worker_id, self.hass_agent_device
            )
            if agent is not None:
                self._hass_agent = agent
                caps = agent.capabilities

        if caps is not None and caps.mac is None:
            mac = await self.hass.async_add_executor_job(
                resolve_mac_via_arp, self.host
            )
            if mac:
                caps = replace(caps, mac=mac)

        if caps is None:
            return

        self.power_capabilities = caps
        self._persist_power_capabilities(caps)

    def _persist_power_capabilities(self, caps: PowerCapabilities) -> None:
        """Store the capabilities in the entry, if they have changed.

        Conditional so as not to rewrite `.storage` on every start-up for
        identical content. The integration deliberately installs no update
        listener (see __init__.py), so this write reloads nothing: it happens
        during setup, and a reload at that moment would loop.
        """
        stored = caps.as_dict()
        if self.entry.data.get(CONF_POWER_CAPS) == stored:
            return
        _LOGGER.debug("Power capabilities of %s remembered: %s", self.host, stored)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_POWER_CAPS: stored}
        )

    async def async_power_command(self, action: str) -> None:
        """Power the machine off or reboot it, through whichever source declared it.

        With HASS.Agent there is nothing to catch: the command goes out over
        MQTT and the broker acknowledges it long before the machine starts
        shutting down.

        Over SSH the opposite holds: the shutdown cuts the session while it is
        running, and depending on when systemd kills sshd, asyncssh may raise
        instead of returning cleanly. That error *is* the expected success, so
        it is not propagated -- otherwise Home Assistant would show a failure
        every time the action worked perfectly.
        """
        caps = self.power_capabilities

        # An empty stored command means this host was established through
        # HASS.Agent. After a degraded start-up the probe has not run, so
        # _hass_agent is still None while the capabilities say otherwise --
        # look the device up now rather than falling through to an SSH command
        # the host has no way to answer.
        if self._hass_agent is None and caps is not None and not caps.command:
            self._hass_agent = await async_probe_hass_agent(
                self.hass, self.worker_id, self.hass_agent_device
            )
            if self._hass_agent is None:
                raise HomeAssistantError(
                    f"{self.host} is driven through HASS.Agent, whose device"
                    " cannot be found right now"
                )

        if self._hass_agent is not None:
            await self._hass_agent.async_press(self.hass, action)
            return

        command = caps.command if caps and caps.command else RIG_POWER_SUDO
        try:
            await self.ssh.async_run(f"{command} {action}", timeout=20)
        except DISCONNECTED_ERRORS:
            _LOGGER.debug(
                "Session cut during '%s %s' on %s: expected",
                command,
                action,
                self.host,
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                f"'{command} {action}' failed on {self.host}: {err}"
            ) from err

    async def _async_get_json(self, url: str) -> dict | list:
        async with self.session.get(url, auth=self._glances_auth) as resp:
            resp.raise_for_status()
            # Glances serves JSON as text/html on some versions, hence
            # content_type=None rather than raising on the MIME type.
            return await resp.json(content_type=None)

    async def async_send_command(self, method: str) -> None:
        """Send pause/resume/stop through the json_rpc API, then refresh state."""
        body = json.dumps({"method": method, "id": 1})
        raw = await self._async_raw_http(
            "POST", "/json_rpc", body, what=f"command {method}"
        )

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

    async def _async_raw_http(
        self, method: str, path: str, body: str, *, what: str
    ) -> bytes:
        """One HTTP/1.0 request to the miner, written by hand.

        A raw client rather than aiohttp, for the writing side only: XMRig's
        server sometimes returns a duplicated response ("Data after Connection:
        close") which aiohttp's strict parser rejects even though the request
        was applied -- a command that worked reported as a failure. Here the
        response is read until close and judged the way curl would, on its
        status line.
        """
        payload = body.encode()
        request = (
            f"{method} {path} HTTP/1.0\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Authorization: Bearer {self._token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + payload

        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_connection(self.host, self.port)
                try:
                    writer.write(request)
                    await writer.drain()
                    return await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"XMRig unreachable for {what}: {err}"
            ) from err

    # --- Mining thread count --------------------------------------------------

    @property
    def cpu(self) -> dict:
        """The summary's CPU block: brand, cores, threads, L3 size."""
        return self.miner.get("cpu") or {}

    @property
    def cpu_backend(self) -> dict:
        """The CPU backend of the last poll: its algorithm and its threads."""
        return (self.data or {}).get("cpu_backend") or {}

    @property
    def mining_threads(self) -> int | None:
        """How many CPU mining threads are running.

        Taken from the CPU backend rather than from the summary's thread list,
        which counts every backend at once (see _async_fetch_cpu_backend); the
        summary is only the fallback, and is right whenever nothing but the CPU
        mines, which is the normal case.

        Either list stays populated while the miner is paused -- the threads
        exist, they are simply not hashing -- so a pause does not make the count
        collapse and move the control on its own.
        """
        threads = self.cpu_backend.get("threads")
        if threads is None:
            threads = (self.miner.get("hashrate") or {}).get("threads")
        if not threads:
            # Empty rather than absent: XMRig serves an empty list in the
            # windows where the backend holds no hashrate object yet -- just
            # after a start, and after the `stop` method. Zero is not a value
            # this control can carry, its minimum being one, so report it as
            # unknown instead of publishing a state below the entity's own
            # minimum.
            return None
        return len(threads)

    @property
    def max_mining_threads(self) -> int | None:
        """The most mining threads XMRig will actually start on this machine.

        min(logical CPUs, L3 / 2 MiB). Asking for more is not refused, it is
        silently ignored -- so without this ceiling the top half of the control
        would do nothing on any machine whose cache, not its core count, is what
        limits it. That is the common case: 8 threads and 8 MiB of L3 gives 4.
        """
        logical = self.cpu.get("threads")
        if not logical:
            return None
        ceiling = int(logical)
        l3 = self.cpu.get("l3")
        if l3:
            ceiling = max(1, min(ceiling, int(l3) // RX_SCRATCHPAD_BYTES))
        # Never below what the miner is already running. The 2 MiB divisor is
        # RandomX's scratchpad, but XMRig's ceiling is per algorithm: a rig on
        # cn-lite (1 MiB) or argon2 (512 KiB) fits more threads than this works
        # out. Rather than carry a table of every algorithm's scratchpad, take
        # the running count as proof that many do fit -- otherwise the control
        # would cap below the value it is itself displaying, and the count the
        # miner started with could not be put back.
        return max(ceiling, self.mining_threads or 0)

    async def async_set_mining_threads(self, threads: int) -> None:
        """Ask the miner for `threads` mining threads, through its own hint.

        The whole configuration is read, edited and written back, because a
        whole configuration is the only thing the API accepts. Two details make
        that safe to do from a slider:

        - Every list-valued key under `cpu` is dropped. Those are the resolved
          per-algorithm thread lists (`rx`, `cn`, `argon2`, ...) that XMRig
          returns in place of the hint, and they take precedence over it:
          leaving them in would write a hint that changes nothing. Dropping them
          hands the decision back to auto-config, which is what works out
          affinity and intensity for the new count -- values worth keeping, as
          they are not contiguous on most machines.
        - Nothing else is touched. Pools, wallet and access token are sent back
          exactly as they arrived, so a rig cannot lose its pool to a slider.
        """
        logical = self.cpu.get("threads")
        if not logical:
            raise HomeAssistantError(
                f"{self.host} has not reported its CPU thread count yet"
            )

        config = await self._async_fetch_config()
        cpu = config.get("cpu")
        if not isinstance(cpu, dict):
            raise HomeAssistantError(
                f"{self.host} returned a configuration with no cpu section"
            )
        dropped = [k for k, value in cpu.items() if isinstance(value, list)]
        for key in dropped:
            del cpu[key]
        cpu["max-threads-hint"] = hint_for_threads(threads, int(logical))
        # Debug rather than a warning: on a miner configured with a hint these
        # keys are auto-config's own output and dropping them is the whole
        # mechanism, so warning would fire on every single change. It is worth
        # recording, because on a hand-tuned miner these same keys are the
        # operator's own thread lists, and this is where they stop existing.
        _LOGGER.debug(
            "Thread profiles handed back to auto-config on %s: %s",
            self.host,
            ", ".join(dropped) or "none",
        )

        raw = await self._async_raw_http(
            "PUT", "/2/config", json.dumps(config), what="the thread count"
        )
        status_line = raw.split(b"\r\n", 1)[0]
        if b" 403" in status_line:
            raise HomeAssistantError(
                f"{self.host} serves a restricted API: set \"restricted\": false"
                " in the miner's http section for the thread count to be"
                " changeable"
            )
        if not (b" 200" in status_line or b" 204" in status_line):
            raise HomeAssistantError(
                "XMRig refused the new thread count: "
                f"{status_line.decode(errors='replace')}"
            )

        # XMRig saves the file and lets its own watcher reload it, so the new
        # count is not readable the instant the PUT returns.
        await asyncio.sleep(CONFIG_RELOAD_DELAY)
        await self.async_request_refresh()

    async def _async_fetch_config(self) -> dict:
        """The miner's running configuration.

        Needs `restricted: false` in the miner's `http` section. A restricted
        API keeps serving /1/summary perfectly happily and answers 403 here,
        which is why that case is named rather than reported as a failure to
        connect.
        """
        try:
            async with asyncio.timeout(10):
                async with self.session.get(
                    f"{self.base_url}/2/config", headers=self._headers
                ) as resp:
                    if resp.status == 403:
                        raise HomeAssistantError(
                            f"{self.host} serves a restricted API: set"
                            ' "restricted": false in the miner\'s http section'
                            " for the thread count to be changeable"
                        )
                    if resp.status == 401:
                        raise HomeAssistantError(
                            f"{self.host} refused the access token"
                        )
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"timed out reading {self.host}'s configuration"
            ) from err
        except aiohttp.ClientError as err:
            raise HomeAssistantError(
                f"could not read {self.host}'s configuration: {err}"
            ) from err

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
