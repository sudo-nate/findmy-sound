#!/usr/bin/env bash
# findmy-sound-ci — run the project's full check suite locally.
#
# Install: symlink or copy this file onto your PATH, e.g.
#   ln -s "$(pwd)/scripts/ci.sh" ~/.local/bin/findmy-sound-ci
#   chmod +x scripts/ci.sh
#
# Then run it from anywhere inside (or pointed at) the repo:
#   findmy-sound-ci
#
# Exits non-zero on the first failing stage. Never touches a real Apple
# Account — all checks here are static or run against mocked objects.

set -euo pipefail

# Resolve the repo root relative to this script, so it works no matter
# where on $PATH it's invoked from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python3}"
FAIL_COUNT=0

section() {
    echo
    echo "== $1 =="
}

fail() {
    echo "FAIL: $1" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

section "Environment check"
"$PY" --version
if ! "$PY" -c "import findmy_sound" >/dev/null 2>&1; then
    echo "findmy_sound is not importable — installing in editable mode..."
    "$PY" -m pip install -e ".[dev]" --break-system-packages 2>/dev/null \
        || "$PY" -m pip install -e ".[dev]"
fi

section "1/5 Syntax check"
if "$PY" -m py_compile src/findmy_sound/*.py tests/*.py; then
    echo "OK: all files compile"
else
    fail "syntax check"
fi

section "2/5 Dependency audit (pip-audit)"
if "$PY" -m pip_audit --version >/dev/null 2>&1; then
    # Only fail the build on vulnerabilities in *this project's* declared
    # dependency, not unrelated packages that happen to be installed
    # elsewhere in the environment.
    AUDIT_OUTPUT="$("$PY" -m pip_audit 2>&1 || true)"
    echo "$AUDIT_OUTPUT"
    if echo "$AUDIT_OUTPUT" | grep -qE "^(pyicloud|srp|keyring|requests|fido2|pydantic|tzlocal|certifi|jeepney|secretstorage)\b"; then
        fail "pip-audit found a vulnerability in the findmy-sound dependency chain"
    else
        echo "OK: no known vulnerabilities in the findmy-sound dependency chain"
    fi
else
    echo "pip-audit not installed; skipping. Install with: pip install pip-audit"
fi

section "3/5 Guardrail checks (no HA imports, web server only in server.py)"
# Only scan *.py sources: the editable install's egg-info (PKG-INFO embeds
# the README, which documents these very guardrails) is not code, and
# grepping all of src/ false-positives on it.
if grep -rIl --include='*.py' "homeassistant" src/findmy_sound/ 2>/dev/null | grep -q .; then
    fail "found a reference to homeassistant in src/ — this project must not import Home Assistant code"
else
    echo "OK: no Home Assistant imports found"
fi
# The HTTP service lives in src/findmy_sound/server.py (stdlib http.server
# only) and nowhere else — a web framework import in any other module is
# still a failure.
FRAMEWORK_FILES="$(grep -rIlE --include='*.py' "\b(http\.server|flask|fastapi|uvicorn|socketserver)\b" src/findmy_sound/ 2>/dev/null | grep -v '^src/findmy_sound/server\.py$' || true)"
if [ -n "$FRAMEWORK_FILES" ]; then
    fail "found a web server framework import outside server.py: $FRAMEWORK_FILES"
else
    echo "OK: no HTTP server framework imported outside server.py"
fi

section "4/5 Mocked test suite (pytest)"
if "$PY" -m pytest tests/ -q; then
    echo "OK: all tests passed"
else
    fail "pytest"
fi

section "5/5 Summary"
if [ "$FAIL_COUNT" -eq 0 ]; then
    echo "All checks passed."
    exit 0
else
    echo "$FAIL_COUNT check(s) failed."
    exit 1
fi
