"""Constants for the XMRig integration."""

DOMAIN = "xmrig_remote_miner"

DEFAULT_PORT = 8080
DEFAULT_SCAN_INTERVAL = 20

# Floor for the poll interval. 0 would make every refresh overdue the moment
# the previous one finished.
MIN_SCAN_INTERVAL = 5

# --- Pool selection -----------------------------------------------------------
#
# The pools a rig may be pointed at, as text: one per line, "Label = url" or a
# bare url. It lives in the config entry because nothing else knows it -- XMRig
# reports the pool it is on and nothing about the alternatives, so the list of
# somewhere-elses has to be written down on this side. See pools.py.
#
# Left empty, no select entity is created: a rig with one pool has no choice to
# offer, and an entity with a single option is a control that cannot control
# anything.
CONF_POOLS = "pools"

# How long to wait after repointing the miner before polling it again. XMRig
# drops the stratum connection and redials on a config reload; asking straight
# away reads the pool it has just left, which shows up as the select snapping
# back to the old value for one poll.
POOL_SWITCH_SETTLE = 3

# Integration-specific config keys (the rest come from homeassistant.const:
# CONF_NAME, CONF_HOST, CONF_PORT, CONF_TOKEN, CONF_SCAN_INTERVAL).
MANUFACTURER = "XMRig"

# Optional system telemetry, served by Glances on the same machine.
# 0 disables polling it entirely.
CONF_GLANCES_PORT = "glances_port"
DEFAULT_GLANCES_PORT = 61208

# Glances' REST API is unauthenticated out of the box, but it can be run behind
# HTTP Basic (`glances -w -u <user>` with a password file), which is how the
# NixOS rigs serve it -- its API exposes the process list and logged-in users,
# so leaving it open while the XMRig API next door demanded a token was an
# asymmetry worth closing. Both fields blank means no credential is sent, which
# is what an unprotected Glances (the Home Assistant add-on, for instance)
# expects.
CONF_GLANCES_USER = "glances_user"
CONF_GLANCES_PASSWORD = "glances_password"

# Glances 4 serves /api/4/<plugin>, Glances 3 serves /api/3/<plugin>. The fields
# read here have the same names in both, so try one then the other at start-up
# and keep whichever answered.
GLANCES_API_VERSIONS = (4, 3)

# The only plugins that say anything useful about a miner.
GLANCES_PLUGINS = ("sensors", "cpu", "mem", "load")

# --- Host machine power control (optional) ------------------------------------
#
# One mechanism, and only one: find the machine's MQTT device and press the
# entities it publishes for itself. A Windows host gets those from HASS.Agent, a
# NixOS rig from the agent in the companion flake. Home Assistant never reaches
# into the machine to run anything; it presses a button the machine offered.
MQTT_DOMAIN = "mqtt"

# The identifier families accepted, in the order they are tried. HASS.Agent
# registers as "hass.agent-<name>" (CreateDeviceConfigModel) and the rig agent
# as "rig-<worker>"; XMRig publishes that same `worker_id` in its summary, and
# it is the machine name in both cases, so the two sides line up without asking
# the user anything.
DEVICE_IDENTIFIER_PREFIXES = ("hass.agent-", "rig-")

# For the cases where the two names have drifted apart, or where several devices
# could plausibly match: an explicit device wins over the automatic lookup.
CONF_MQTT_DEVICE = "mqtt_device"

# The command vocabulary. It is HASS.Agent's -- its built-in commands are named
# Shutdown, Restart and Sleep -- and the rig agent publishes exactly those words
# deliberately, so that one table serves both operating systems. The entities
# surface as `button`s in current HASS.Agent and as `switch`es in older
# versions; mqtt_power.py accepts both and calls the matching service.
POWER_SHUTDOWN_KEYS = ("shutdown",)
POWER_REBOOT_KEYS = ("restart", "reboot")
# Suspend-to-RAM is called "Sleep" on the wire. Hibernate is deliberately not in
# here: it is S4, which writes RAM to disk and loses the warm dataset that is
# the entire reason for offering suspend at all -- so it would cost as much as
# a poweroff while looking like it costs nothing.
POWER_SUSPEND_KEYS = ("sleep",)

# Waking is the one action that cannot travel over MQTT: a machine that is off
# runs no client. The MAC normally comes from the MQTT device's `connections`,
# which the rig agent fills in during discovery. HASS.Agent does not fill it in,
# hence this optional field for Windows hosts.
CONF_MAC = "mac"

# Actions a machine can announce.
ACTION_OFF = "off"
ACTION_REBOOT = "reboot"

# Suspend to RAM. Worth a verb of its own rather than being folded into "off"
# because what it costs is completely different, and that difference is the
# whole reason it exists.
#
# A poweroff loses the RandomX dataset and the hugepage pool, so coming back
# means a full boot plus a dataset init -- tens of seconds, more without 1 GB
# pages. S3 keeps RAM refreshed, so both survive and the machine resumes in a
# few seconds with the miner already warm. For a rig that is stopped and started
# several times a day to follow solar surplus, that is the difference between a
# sensible thing to do and a permanent tax on doing it.
#
# It wakes through the same magic packet as an off machine, so it adds no new
# way to lose a rig. What it does add is a per-board firmware risk: a board that
# sleeps but does not resume is a walk to the machine. The rig only publishes
# the Sleep button when its own config says the board has been tested.
ACTION_SUSPEND = "suspend"

WOL_PORT = 9

# --- Remembered power capabilities --------------------------------------------
#
# The power probe only runs when the entry starts up, and it needs a machine
# whose MQTT device is already known to Home Assistant. Yet the button that
# matters most is wake, whose entire purpose lies *while the machine is off*: if
# the probe result lived only in memory, a Home Assistant restart while a rig
# was off would produce an entry with no buttons at all -- exactly when one is
# needed.
#
# The result is therefore stored in the config entry, which survives restarts,
# and the buttons are built from that. The probe now only discovers and
# corrects.
CONF_POWER_CAPS = "power_caps"
CAPS_ACTIONS = "actions"
CAPS_MAC = "mac"

# --- States of the `state` sensor ---------------------------------------------
#
# Enum values, so they are fixed identifiers rather than prose: a sensor's state
# is a key, not a label. The displayed labels live in the translation files,
# under `entity.sensor.state.state`.
STATE_MINING = "mining"
STATE_PAUSED = "paused"

# Two more, and neither of them comes from XMRig -- they come from the machine
# itself, over MQTT. They exist because "the miner did not answer" is not a
# state: a rig switched off on purpose and a rig whose power was cut both stop
# answering, and which one it is decides whether anybody needs to go and look.
#
# `off` is read from a machine that announced `shutting-down` on its way out
# and then stopped answering. The announcement is retained, so it is still
# there hours later, which is what makes this readable at all rather than a
# three-second window nobody was watching.
STATE_OFF = "off"

# `lost` is the will, published by the broker on the machine's behalf because
# the machine never got to say anything. Power cut, network gone, kernel panic.
STATE_LOST = "lost"

# What the machine publishes, as opposed to what this integration calls it. The
# mapping is deliberate rather than passthrough: `shutting-down` is true for the
# three seconds it takes to stop, and misleading for the ten hours afterwards.
MACHINE_SHUTTING_DOWN = "shutting-down"
MACHINE_OFFLINE = "offline"

MACHINE_STATES = {
    MACHINE_SHUTTING_DOWN: STATE_OFF,
    MACHINE_OFFLINE: STATE_LOST,
}

# Remembered for the same reason as the power capabilities next to it: the
# worker name arrives in the XMRig summary, so a Home Assistant restarted while
# a rig was off would not know what to look for -- precisely when the answer
# matters.
CONF_WORKER_ID = "worker_id"
