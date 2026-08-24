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
| `number.mining_threads` | How many CPU threads the miner may use, 1 to what this machine can actually run. Applied live — see below. |
| `sensor.hashrate` | H/s, 10-second window |
| `sensor.hashrate_max` | H/s, peak since start |
| `sensor.shares_good` | accepted shares |
| `sensor.shares_rejected` | rejected shares |
| `sensor.uptime` | seconds |
| `sensor.pool` | pool currently connected to |
| `sensor.ping` | pool round-trip, ms |
| `sensor.state` | miner state |
| `sensor.os` | operating system, read from XMRig's own user-agent string |
| `sensor.memory_total` | total RAM, which is what makes the memory-usage percentage mean something |

And, when [Glances](https://nicolargo.github.io/glances/) is reachable on the
same machine, four more — the numbers XMRig does not report but that explain
most of the hashrate you are looking at:

| Entity | Notes |
|---|---|
| `sensor.cpu_temperature` | °C. Thermal throttling is the usual reason a rig quietly loses hashrate. |
| `sensor.cpu_usage` | % |
| `sensor.memory_usage` | % |
| `sensor.load` | 15-minute load average as a percentage of core count, so it compares across machines |

`sensor.os` and `sensor.memory_total` come out of the summary that is fetched
anyway, so they cost no extra request. They are deliberately not read from
Glances' `system` plugin, which would be tidier but would add an HTTP call on
every poll for two facts that never change. RAM *speed* and DIMM layout are in
neither source — they need `dmidecode` and root — so they are not here.

Plus host power buttons, when the machine supports them.

The switch pauses the *miner*, which is not the same as powering the machine —
that is what the buttons below are for.

## Mining threads

`number.mining_threads` changes how much of the CPU the miner takes, while it
runs. The pool connection and the RandomX dataset survive the change: the CPU
backend restarts in about 3 ms, so there is no re-initialisation to pay and no
share lost.

It is a **thread count**, not a percentage, although XMRig's own setting is a
percentage. `cpu.max-threads-hint` is a percentage of the machine's *logical*
CPUs, which auto-config then caps at what the L3 cache can hold — 2 MiB per
RandomX thread. On an 8-thread CPU with 8 MiB of L3 that ceiling is 4 threads,
so 50%, 75% and 100% all mean the same thing and the top half of a percentage
slider would do nothing. The integration therefore offers 1 to
`min(logical CPUs, L3 / 2 MiB)` and converts:

```
hint = round(threads × 100 / logical CPUs)
```

XMRig still decides everything a thread count implies — which cores to pin to,
at what intensity. The resolved thread lists are dropped from the config before
it is written back precisely so that auto-config gets to make that decision
again; on most machines the affinities it picks are not contiguous, and writing
an explicit list would throw that away.

**How long it lasts depends on your miner, not on Home Assistant.** XMRig
applies a config change by saving it over the file it was started from and
letting its own watcher reload it. So the new count lasts exactly as long as
that file does:

| Miner | Outcome |
|---|---|
| [NixOS rig](https://github.com/IonCojucari/nixos-xmrig-flake) | resets at the next miner start — `xmrig-start` rebuilds the config from the declared `maxThreadsHint` |
| [HAOS add-on](https://github.com/IonCojucari/haos-xmrig) | resets at the next start — `run.sh` rebuilds the config from the add-on options |
| Plain XMRig started from your own `config.json` | persists — that file has been rewritten |

That rewrite is a full save, so on a hand-tuned miner it also replaces any
explicit per-algorithm thread lists (`"rx": [0, 2, 4, 6]` and the like) with
auto-config's own, and drops comments the way any save does. Worth a backup of
`config.json` before driving the count from Home Assistant.

Requires `"restricted": false` in the miner's `http` section, which is also what
the pause switch needs. A restricted API keeps serving the sensors and answers
403 to this; the error says so.

## Host power control (optional)

Three more entities appear when the host turns out to support them:

| Entity | Mechanism |
|---|---|
| `button.shutdown` | `rig-power off` over SSH, or a HASS.Agent command |
| `button.reboot` | `rig-power reboot` over SSH, or a HASS.Agent command |
| `button.wake` | Wake-on-LAN magic packet |

Detected the same way Glances is, at setup, from two sources tried in order.

**1. SSH and `rig-power`.** One session, and the machine is asked what it
accepts:

```
sudo -n rig-power status   ->  actions: off, reboot, status
```

Only the verbs it actually names become buttons. Nothing is probed by
attempting it — asking a machine to power off in order to discover whether it
may is not an acceptable probe, which is why the wrapper has a `status` verb.
The same session reads the MAC of the interface holding the default route, so
Wake-on-LAN cannot pick the wrong port on a machine with several NICs.

`sudo -n rig-power status` is tried first and plain `rig-power status` second,
for hosts that have no `sudo`. The form that answered is the form used to act.

**2. HASS.Agent**, if the first found nothing. Meant for Windows, which has
neither `sudo` nor `rig-power`, and where providing them means installing
OpenSSH Server, placing the key correctly — for an administrator account that
is `C:\ProgramData\ssh\administrators_authorized_keys`, not
`~/.ssh/authorized_keys` — and writing a wrapper. If
[HASS.Agent](https://github.com/hass-agent/HASS.Agent) already runs there, all
of that exists by another route, so its commands are pressed instead.

Those commands are `button` entities in current HASS.Agent and `switch` entities
in older ones. Both are accepted, and the right service is called for each —
accepting only one form fails exactly like a missing command: the device is
found, no buttons appear, and nothing says why.

Matching is automatic: HASS.Agent registers as `hass.agent-<name>` and XMRig
publishes `worker_id`, which is the machine name on both sides. The optional
*HASS.Agent device* field in the config flow is only for when those two names
have drifted apart.

Two things to know:

- **The commands are not there by default.** Shutdown and Restart have to be
  added in HASS.Agent's own *Commands* tab. A device found with no shutdown
  command yields no buttons rather than buttons that do nothing.
- **The entities then exist twice**, HASS.Agent's own command and the button
  here. That is the price of one device per rig in Home Assistant — the same
  trade-off as reading Glances directly instead of adding the official
  integration alongside.

Use the plain `hass-agent/HASS.Agent` project: the original
`LAB02-Research/HASS.Agent` has had no release since 2022.

Waking cannot go over SSH — the machine is off, nothing is listening — so it is
a UDP broadcast instead, and it is a button rather than half of a switch:
neither half reports state, and a switch would promise one it cannot read.

When the machine could not name its own MAC — a Windows host, or one reached
through HASS.Agent — it is resolved by ARP from Home Assistant instead, via
`getmac`. Strictly a fallback, and only to complete capabilities some source
has already established: it can add a wake button to a host that has power
buttons, never create one on a host that has none. It also needs Home Assistant
to share the machine's layer-2 segment, which is true on a LAN and false across
a VPN or a routed subnet — the same condition the magic packet itself has.

`rig-power` comes from the companion
[NixOS flake](https://github.com/IonCojucari/nixos-xmrig-flake), which grants
the account that one wrapper and nothing else. Any host offering an equivalent
command works just as well.

The probe result is remembered in the config entry, and the buttons are built
from what is remembered rather than from a live probe. That is what makes the
wake button usable at all: probing needs a machine that answers, waking is only
ever wanted when none does. Previously the probe lived in memory only, so a
Home Assistant restart while a rig was off produced an entry with no buttons —
precisely when one was needed. An entry with remembered capabilities now sets
up even when the miner is unreachable: buttons live, sensors unavailable, and
it reloads itself as soon as the miner answers again. A probe that fails never
erases what was known; only a successful one writes.

**Caveats.** Host key verification is disabled (`known_hosts=None`): these are
LAN machines whose host key changes on reinstall, and the alternative is asking
you to maintain a `known_hosts` file inside a container. The residual risk is a
LAN neighbour spoofing the rig's address, and what they gain is the ability to
power it off. `asyncssh` and `getmac` are pulled in as requirements; if either is
unavailable the affected capability is skipped rather than breaking the
integration.

## System telemetry (optional)

Glances is polled on port 61208 if something answers there. Fill in the Glances
username and password when it runs behind HTTP Basic — which is how the NixOS
flake serves it, since that API hands out the process list and the logged-in
users, not just a temperature. Leave both blank for an unprotected instance,
such as the Home Assistant add-on. Credentials that are refused log one warning
and disable the system sensors, rather than looking identical to a machine with
no Glances at all.

It is strictly optional and deliberately unable to break anything:

- if Glances does not answer when the entry is set up, the four system sensors
  are simply never created — the integration works as before, with fewer
  entities rather than four permanently-unavailable ones;
- if Glances stops answering later, those sensors go unavailable and the miner
  sensors keep updating. A telemetry outage must not take the hashrate reading
  down with it;
- set the Glances port to `0` to disable it entirely.

Only four plugins are read (`sensors`, `cpu`, `mem`, `load`), and both API
versions are handled — Glances 4 serves `/api/4/…`, Glances 3 `/api/3/…`, with
the same field names for what is read here.

CPU temperature is picked by label — `Package id` on Intel, `Tdie`/`Tctl` on
AMD, then the hottest `Core N` — rather than by taking the maximum of every
probe. Glances reports chassis (`acpitz`), chipset (`pch_*`) and DIMM (`jc42`)
probes under the same `temperature_core` type, so a plain maximum reports the
RAM temperature as the CPU's as soon as the miner stops and the package cools.
No reading is better than a plausible wrong one.

Home Assistant also ships an official Glances integration, which exposes far
more. Use it instead if you want the full picture; the point of reading Glances
here is to get temperature and load onto the *same device* as the hashrate,
without two devices per rig.

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
false` is what enables `pause`/`resume` — which is also why the token is not
optional: without it, anything on the network could pause the miner.

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
(default 20 s), plus the optional Glances port and credentials. Everything is
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
