#!/usr/bin/env bash
# Fixed, argument-free entry point for the "me" profile. Delegates to the
# real implementation in ring-my-device.sh — see that file's header and
# the README's "Headless invocation" section for setup instructions.
exec "$(dirname "$0")/ring-my-device.sh" me
