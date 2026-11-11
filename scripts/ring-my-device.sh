#!/usr/bin/env bash
# ring-my-device.sh — trigger Play Sound on one pre-configured device, with
# zero interactivity. Designed to be invoked by something else (a Siri
# Shortcut's "Run Script Over SSH" action, a cron job, a systemd/launchd
# unit, a webhook relay triggered by an ESP32, etc.) rather than by a human
# sitting at a terminal.
#
# It does NOT prompt for a password (must already be saved in the OS
# keychain from a prior interactive `findmy devices` run) and does NOT ask
# for confirmation (`--yes` is always passed). Point this at a device you're
# comfortable triggering unattended.
#
# Supports multiple people/devices via a "profile" name (first argument).
# Each profile gets its own config file, log file, and lock, so ringing
# one person's phone never blocks or interferes with another's.
#
# Setup (once per profile):
#   1. Run `findmy --apple-id you@icloud.com devices` interactively at least
#      once so the password is saved to your OS keychain and, if needed,
#      2FA has been completed and the session trusted. (If both people use
#      the same Apple ID / Family Sharing, you only need to do this once —
#      both device IDs will show up in the same `devices` listing. If they
#      use separate Apple IDs, do this once per Apple ID.)
#   2. cp config/ring.env.example ~/.config/findmy-sound/ring-<profile>.env
#      chmod 600 ~/.config/findmy-sound/ring-<profile>.env
#      # edit it: set APPLE_ID and DEVICE_ID for that person's device
#   3. chmod +x scripts/ring-my-device.sh
#
# Invoke:
#   scripts/ring-my-device.sh me
#   scripts/ring-my-device.sh partner
#
# Calling with no profile argument at all falls back to the original
# single-device file layout (~/.config/findmy-sound/ring.env) for backward
# compatibility with existing setups.
#
# Exit codes:
#   0  success
#   2  config file missing or incomplete
#   3  findmy executable not found
#   4  another run for this profile is already in progress; request ignored
#   *  whatever findmy itself exited with (auth/session/device errors)

set -euo pipefail

PROFILE="${1:-}"

if [ -n "$PROFILE" ]; then
    DEFAULT_CONFIG="$HOME/.config/findmy-sound/ring-${PROFILE}.env"
    DEFAULT_LOG="$HOME/.local/state/findmy-sound/ring-${PROFILE}.log"
    DEFAULT_LOCK="${TMPDIR:-/tmp}/findmy-sound-ring-${PROFILE}.lock.d"
else
    DEFAULT_CONFIG="$HOME/.config/findmy-sound/ring.env"
    DEFAULT_LOG="$HOME/.local/state/findmy-sound/ring.log"
    DEFAULT_LOCK="${TMPDIR:-/tmp}/findmy-sound-ring.lock.d"
fi

CONFIG_FILE="${RING_CONFIG:-$DEFAULT_CONFIG}"
LOG_FILE="${RING_LOG:-$DEFAULT_LOG}"
LOCK_DIR="${RING_LOCK_DIR:-$DEFAULT_LOCK}"

mkdir -p "$(dirname "$LOG_FILE")"

timestamp() {
    # Portable across macOS (BSD date) and Linux (GNU date) — unlike
    # `date -Iseconds`, which only GNU date supports.
    date "+%Y-%m-%dT%H:%M:%S%z"
}

log() {
    echo "$(timestamp) $*" >>"$LOG_FILE"
}

if [ ! -f "$CONFIG_FILE" ]; then
    log "ERROR: config file not found at $CONFIG_FILE"
    echo "Config file not found: $CONFIG_FILE" >&2
    echo "Copy config/ring.env.example to that path and fill in APPLE_ID + DEVICE_ID." >&2
    exit 2
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

if [ -z "${APPLE_ID:-}" ] || [ -z "${DEVICE_ID:-}" ]; then
    log "ERROR: APPLE_ID or DEVICE_ID not set in $CONFIG_FILE"
    echo "APPLE_ID and DEVICE_ID must both be set in $CONFIG_FILE" >&2
    exit 2
fi

# Prefer a venv sitting next to this script; fall back to PATH.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

if [ -x "$REPO_ROOT/.venv/bin/findmy" ]; then
    FINDMY_BIN="$REPO_ROOT/.venv/bin/findmy"
elif command -v findmy >/dev/null 2>&1; then
    FINDMY_BIN="$(command -v findmy)"
else
    log "ERROR: findmy executable not found ($REPO_ROOT/.venv/bin or PATH)"
    echo "findmy executable not found. Run: pip install -e . inside a venv." >&2
    exit 3
fi

# Simple, portable lock (mkdir is atomic on every POSIX filesystem; unlike
# flock, this doesn't require util-linux, so it works on macOS out of the
# box too). Prevents a double-tapped button or overlapping triggers from
# firing two requests at once.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: another run already in progress ($LOCK_DIR exists)"
    echo "Another ring request is already in progress; ignoring this one." >&2
    exit 4
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

log "START: triggering play-sound for device ${DEVICE_ID:0:8}..."

if "$FINDMY_BIN" --apple-id "$APPLE_ID" play-sound --device-id "$DEVICE_ID" --yes >>"$LOG_FILE" 2>&1; then
    log "OK: play-sound succeeded"
    exit 0
else
    status=$?
    log "FAIL: play-sound exited with status $status (see $LOG_FILE)"
    exit "$status"
fi
