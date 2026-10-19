# XMRig Remote Miner — Home Assistant integration

Custom integration exposing an [XMRig](https://xmrig.com) miner's HTTP API to
Home Assistant. Local polling, config flow, no cloud.

It talks to XMRig, not to the machine XMRig runs on, so it works against any
correctly configured instance — Linux, Windows, macOS, a container, a Raspberry
Pi — regardless of how that host was set up. Add one entry per miner.

## Entities

One device per miner, each with:

| Entity | Notes |
|---|---|
| `switch` | ON = mining, OFF = paused. Uses the API's `pause`/`resume` methods, so the process stays alive and the RandomX dataset stays allocated — resuming is instant. |
| `sensor.hashrate_khs` | kH/s, 10-second window |
| `sensor.hashrate_max` | kH/s, peak since start |
| `sensor.shares_good` | accepted shares |
| `sensor.shares_rejected` | rejected shares |
| `sensor.uptime` | seconds |
| `sensor.pool` | pool currently connected to |
| `sensor.ping` | pool round-trip, ms |
| `sensor.state` | miner state |
| `sensor.os` | operating system, read from XMRig's own user-agent string |
| `sensor.memory_total` | total RAM, which is what makes the memory-usage percentage mean something |

| `sensor.memory_usage` | % of RAM in use, from XMRig's own `resources` block |
| `sensor.load` | 15-minute load average as a percentage of core count, so it compares across machines. Unavailable on Windows, which has no load average. |

All of these come out of the one summary request made on every poll.
`sensor.os` is read from XMRig's user-agent string, so it costs no request of
its own. RAM *speed* and DIMM layout are not in the summary — they need
`dmidecode` and root — so they are not here.

And, when the machine's own agent publishes a CPU temperature, one more:

| Entity | Notes |
|---|---|
| `sensor.cpu_temperature` | °C, mirrored from the machine's MQTT device — see [CPU temperature](#cpu-temperature). Thermal throttling is the usual reason a rig quietly loses hashrate. |

And, when the entry lists more than one pool, one more:

| Entity | Notes |
|---|---|
| `select.pool` | which pool the rig mines to — see below |

Plus host power buttons, when the machine supports them.

The switch pauses the *miner*, which is not the same as powering the machine —
that is what the buttons below are for.

## Changing the pool

List the pools you want to be able to choose between in the config flow, one
per line, and a `select` appears on each rig:

```
pool.supportxmr.com:443
Nanopool = xmr-eu1.nanopool.org:14433
MoneroOcean = gulf.moneroocean.stream:20128
```

The label before `=` is optional; without one, the address is the label. Each
label has to be unique, since the select offers pools by name. Fewer than two
lines means there is nothing to choose between, and no select entity
is created — a control with a single option is a control that cannot control
anything.

Picking one reads the miner's live configuration, replaces the first pool's
address, and pushes it back through the API's `/2/config` endpoint. Everything
else XMRig was told is carried over untouched — the wallet, the worker name,
any further pools, which are its failover targets.

TLS belongs to the pool, not to the rig, so it follows the address you pick.
Write the scheme when the pools differ — `stratum+ssl://host:443` turns TLS on,
`stratum+tcp://host:3333` turns it off — and a bare `host:port` keeps whatever
the rig was using. A pinned `tls-fingerprint` is dropped when the host changes,
since it names one server's certificate. The switch
costs a stratum reconnect, not a restart: the RandomX dataset is not rebuilt
for a move between pools on the same chain, so the hashing never stops.

The select shows the pool you asked for while the miner reconnects, then the
pool the miner reports. If the two have not agreed after a few polls it drops
back to the truth, which is how a pool refusing the login looks from here.

A rig sitting on a pool that is not in your list — one repointed by hand, or
left on a pool since dropped from the list — shows that pool as the selection
anyway. It is not offered as a destination; it is just where the rig is, and
saying so is worth more than showing nothing.

### Making it survive a restart

`/2/config` changes the miner that is running. Whether the choice outlives that
process is the miner's business, not this integration's: XMRig starts from
whatever config file it is pointed at.

The companion [NixOS flake](https://github.com/IonCojucari/nixos-xmrig-flake)
handles it — its config lives in `/var/lib/xmrig`, XMRig writes the pushed
config back there, and each start carries the chosen pool into the freshly
generated config unless the flake's own pool value has changed since, in which
case the rebuild wins. On any other setup, run XMRig with `"autosave": true`
and a config file it can write, or expect the pool to revert at the next
restart.

## Host power control (optional)

Four more entities appear when the machine supports them:

| Entity | Mechanism |
|---|---|
| `button.shutdown` | presses the machine's own *Shutdown* entity |
| `button.reboot` | presses its *Restart* entity |
| `button.suspend` | presses its *Sleep* entity |
| `button.wake` | Wake-on-LAN magic packet |

There is one mechanism, and it is the machine's. Each host publishes its own
Home Assistant entities over MQTT discovery — [HASS.Agent](https://github.com/hass-agent/HASS.Agent)
does it on Windows, the agent in the companion
[NixOS flake](https://github.com/IonCojucari/nixos-xmrig-flake) does it on the
rigs — and this integration simply presses them. Home Assistant never opens a
session on the machine and never runs a command there: the machine executes its
own poweroff, having offered the button itself.

That is also why only the verbs a host actually announces become buttons. A rig
that has not declared its board safe to suspend publishes no Sleep entity, and
no Suspend button appears.

### Suspend, and why it is a separate verb

Suspend to RAM wakes on the same magic packet as a shut-down machine, so it
adds no new way to lose a rig. What it changes is the cost of coming back:

| | Saves | Costs to resume |
|---|---|---|
| Pause (the switch) | part of the draw — the machine stays awake | instant; pool connection and RandomX dataset both kept |
| **Suspend** | nearly all of it | seconds; RAM is refreshed, so the dataset and the hugepage pool survive |
| Shut down | all of it | a full boot plus a fresh dataset init |

That matters for a rig switched on and off through the day — following solar
surplus, say — where a poweroff spends a minute of every cycle rebuilding
something that was already in memory.

Leave suspend off for a board you have not tested: S3 is firmware, and a board
that sleeps but does not resume is a walk to the machine — the wake button
would be pressing against nothing.

### Finding the machine's device

Matching is automatic. HASS.Agent registers as `hass.agent-<name>`, the rig
agent as `rig-<worker>`, and XMRig publishes that same `worker_id`, which is
the machine name in both cases. The optional *MQTT device* field in the config
flow is only for when those two names have drifted apart.

The command entities are `button`s in current agents and `switch`es in older
HASS.Agent versions. Both are accepted, and the right service is called for
each — accepting only one form fails exactly like a missing command: the device
is found, no buttons appear, and nothing says why.

Two things to know about HASS.Agent specifically:

- **The commands are not there by default.** Shutdown and Restart have to be
  added in its own *Commands* tab. A device found with no command entity yields
  no action buttons rather than buttons that do nothing.
- **The entities then exist twice**, HASS.Agent's own command and the button
  here. That is the price of having every control for a rig on the rig's own
  device in Home Assistant.

**Hide the duplicates, do not disable them.** This applies to every agent, the
rigs included. The buttons here work by pressing the machine's own entity, so
disabling that entity leaves them with nothing to press — and Home Assistant
does not treat a service call naming a disabled entity as an error, it just
logs a line and returns, while the button has already stamped itself as
pressed. The press then looks like it worked and the machine stays on. Hiding
removes the duplicate from the dashboard and keeps it pressable, which is what
was wanted in the first place.

Since 0.13.0 a disabled command is found anyway and the button says so instead
of failing silently, and it no longer takes the rig's remembered capabilities
down with it. The fix is still to re-enable the entity.

Use the plain `hass-agent/HASS.Agent` project: the original
`LAB02-Research/HASS.Agent` has had no release since 2022.

### Waking

Waking is the one action that cannot travel over MQTT — the machine is off, it
runs no client — so it is a UDP broadcast instead, and a button rather than
half of a switch: neither half reports state, and a switch would promise one it
cannot read.

The MAC comes from the MQTT device itself: the rig agent puts it in the
`connections` field of its discovery payload, read from the interface holding
the default route, so Wake-on-LAN cannot pick the wrong port on a machine with
several NICs. HASS.Agent declares no connections, so for a Windows host the
optional *MAC address* field in the config flow supplies it — and failing both,
whatever MAC was last remembered for the machine is kept. A device that names
its own MAC wins over the other two, which are typed by hand or inherited and
cannot notice a replaced network card; but a probe finding no MAC never erases
one, or a working wake button would vanish the moment nothing republished it.

The magic packet needs Home Assistant to share the machine's layer-2 segment,
which is true on a LAN and false across a VPN or a routed subnet.

### What is remembered

The probe result is stored in the config entry, and the buttons are built from
what is remembered rather than from a live probe. That is what makes the wake
button usable at all: waking is only ever wanted when the machine is off. An
entry with remembered capabilities sets up even when the miner is unreachable —
buttons live, sensors unavailable — and reloads itself as soon as the miner
answers again. A probe that finds nothing never erases what was known; only a
successful one writes.

## CPU temperature

XMRig does not report temperatures, so the machine's own agent publishes one on
its MQTT device, next to the power commands, and this integration mirrors it onto
the rig's device. It is found on that device by what it measures — a
`temperature` device class, or a unit of °C — and only created if it exists
when the entry sets up.

**Windows (HASS.Agent).** Run [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
on the machine. It makes its readings available over WMI. Then add a *WMI
Query* sensor in HASS.Agent's *Sensors* tab:

- Scope: `root\LibreHardwareMonitor`
- Query: `SELECT Value FROM Sensor WHERE SensorType='Temperature' AND Name='CPU Package'`

AMD CPUs name it `Core (Tctl/Tdie)` rather than `CPU Package`. LibreHardwareMonitor's
own window shows the exact name. Windows' built-in
`MSAcpi_ThermalZoneTemperature` class is not a substitute: on most boards it
reports an ACPI zone, not the CPU, and often a constant.

**NixOS rigs.** The rig agent in the companion flake publishes the package
temperature itself, read from hwmon by label (`Package id 0` on Intel,
`Tctl`/`Tdie` on AMD).

## Requirements on the miner

The HTTP API has to be enabled, tokenised, and unrestricted:

```json
"http": {
  "enabled": true,
  "host": "0.0.0.0",
  "port": 8080,
  "access-token": "…",
  "restricted": false
}
```

`restricted: true` (the default) serves read-only summary data, which is enough
for every sensor here but leaves the switch unable to do anything. `restricted:
false` is what enables `pause`/`resume`, and the `/2/config` read and write the
pool selector needs — which is also why the token is not optional: without it,
anything on the network could pause the miner or point it at a stranger's
wallet.

So a rig this integration can already pause can be repointed too, and one that
refuses the first refuses the second. Selecting a pool on a restricted miner
fails with a message saying exactly that rather than silently doing nothing.

Bind to `0.0.0.0` only on a network you trust, and firewall the port to your
LAN.

## Installation

Requires **Home Assistant 2025.3 or newer** — the entity platforms use
`AddConfigEntryEntitiesCallback`, which does not exist before that release.

**HACS** — add `https://github.com/IonCojucari/homeassistant-xmrig` as a
custom repository of category *Integration*, install, then restart Home
Assistant.

**By hand** — copy `custom_components/xmrig_remote_miner/` into
`<config>/custom_components/` and restart.

Either way, add it from *Settings → Devices & Services → Add Integration →
XMRig Remote Miner*.

## Configuration

Per miner: host, port (default 8080), access token, and poll interval
(default 20 s), plus an optional MAC
address for Wake-on-LAN, an optional MQTT device when auto-detection cannot
find it, and the optional list of pools to choose between. Everything is
stored in the config entry, never in this repo.

Everything is also editable afterwards, through *Reconfigure* on the rig's
entry. That matters more than it sounds: the entities' `unique_id`s derive from
the config entry, so deleting and re-adding a rig to change one field would
rename every entity and drop its long-term statistics.

## Language

Sources, comments and UI strings are in English. French translations ship in
`translations/fr.json`; further languages are welcome as pull requests.

## License

MIT — see [LICENSE](LICENSE).
