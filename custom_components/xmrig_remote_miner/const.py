"""Constants for the XMRig integration."""

DOMAIN = "xmrig_remote_miner"

DEFAULT_PORT = 8080
DEFAULT_SCAN_INTERVAL = 20

# Floor for the poll interval. 0 would make every refresh overdue the moment
# the previous one finished.
MIN_SCAN_INTERVAL = 5

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
# Detected when the entry is added, like Glances: open an SSH session, ask
# `rig-power status` what it accepts, and create only the matching buttons. A
# host without rig-power gets none.
CONF_SSH_USER = "ssh_user"
CONF_SSH_PORT = "ssh_port"
CONF_SSH_KEY = "ssh_key"

DEFAULT_SSH_USER = "ha"
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_KEY = "/config/.ssh/id_ed25519"

# The `status` verb was added to rig-power for this: asking what is permitted
# without having to attempt it. Powering a machine off to discover whether you
# were allowed to is not an acceptable probe.
RIG_POWER_PROBE = "sudo -n rig-power status"

# Fallback without sudo, for machines exposing `rig-power` directly -- typically
# Windows, which has neither sudo nor an equivalent. The sudo form is always
# tried first: it is what every existing rig uses, and it must stay the nominal
# path.
RIG_POWER_PROBE_PLAIN = "rig-power status"

# Matching prefixes for running the action once the probe has passed.
RIG_POWER_SUDO = "sudo -n rig-power"
RIG_POWER_PLAIN = "rig-power"

# Reads the MAC of the interface holding the default route, for Wake-on-LAN.
# Saves one more config field, and above all avoids picking the wrong card on
# machines that have several.
MAC_PROBE = (
    "ip -o link show "
    "\"$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p')\" "
    "2>/dev/null | sed -n 's|.*link/ether \\([0-9a-f:]*\\).*|\\1|p'"
)

# The MAC getmac returns when ARP resolution failed without raising.
MAC_UNRESOLVED = "00:00:00:00:00:00"

# --- Power control via HASS.Agent (Windows machines) --------------------------
#
# When the SSH probe finds nothing, the machine may already expose its shutdown
# through HASS.Agent, which publishes its commands over MQTT. Nothing is
# reimplemented here: its device is looked up in the registry and its own
# entities are pressed.
#
# HASS.Agent registers with Identifiers = "hass.agent-<name>" (see
# CreateDeviceConfigModel), and XMRig publishes `worker_id` in /2/summary, which
# defaults to the machine name on Windows as on Linux. The two therefore line up
# without asking the user anything -- with the field below for the cases where
# they have drifted apart.
CONF_HASS_AGENT_DEVICE = "hass_agent_device"

HASS_AGENT_IDENTIFIER_PREFIX = "hass.agent-"
MQTT_DOMAIN = "mqtt"

# HASS.Agent's built-in commands are named like this. They surface as `button`
# entities in current versions and as `switch` entities in older ones;
# hass_agent.py accepts both and calls the matching service.
HASS_AGENT_SHUTDOWN_KEYS = ("shutdown",)
HASS_AGENT_REBOOT_KEYS = ("restart", "reboot")

# Actions rig-power can announce.
ACTION_OFF = "off"
ACTION_REBOOT = "reboot"

WOL_PORT = 9

# --- Remembered power capabilities --------------------------------------------
#
# The power probe only runs when the entry starts up, and it needs a machine
# that is switched on in order to answer. Yet the button that matters most is
# wake, whose entire purpose lies *while the machine is off*: if the probe
# result lived only in memory, a Home Assistant restart while a rig was off
# would produce an entry with no buttons at all -- exactly when one is needed.
#
# The result is therefore stored in the config entry, which survives restarts,
# and the buttons are built from that. The probe now only discovers and
# corrects.
CONF_POWER_CAPS = "power_caps"
CAPS_ACTIONS = "actions"
CAPS_MAC = "mac"
CAPS_COMMAND = "command"

# --- States of the `state` sensor ---------------------------------------------
#
# Enum values, so they are fixed identifiers rather than prose: a sensor's state
# is a key, not a label. The displayed labels live in the translation files,
# under `entity.sensor.state.state`.
STATE_MINING = "mining"
STATE_PAUSED = "paused"
