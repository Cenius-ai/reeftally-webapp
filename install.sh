#!/usr/bin/env bash
#
# ReefTally setup.
#
# Runs as an unprivileged user, installs only project-level dependencies (the
# tool is standard-library only, so there are none to fetch), verifies that the
# entry point compiles and imports, runs the developer smoke suite, and EXITS.
# It never starts a server, never calls sudo, and never touches a system package
# manager.
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

echo "==> ReefTally setup"

# 1. Interpreter check. Python itself is a documented prerequisite; this script
#    never installs it.
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: '$PYTHON' was not found on PATH; Python 3.8+ is required (see INSTALL.md)" >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "error: Python 3.8 or newer is required; found $("$PYTHON" --version 2>&1)" >&2
    exit 1
fi
echo "==> $("$PYTHON" --version 2>&1) on $("$PYTHON" -c 'import sys; print(sys.platform)')"

# 2. Dependencies. requirements.txt declares the (empty) set of third-party
#    packages; nothing is downloaded, so setup works offline.
if [ -f requirements.txt ] && grep -qvE '^\s*(#|$)' requirements.txt; then
    echo "==> installing declared dependencies"
    "$PYTHON" -m pip install --upgrade pip setuptools wheel
    "$PYTHON" -m pip install -r requirements.txt
else
    echo "==> no third-party dependencies to install (standard library only)"
fi

# 3. Build check: compile and import the entry point so an incomplete tree fails
#    here rather than at first use.
"$PYTHON" -m py_compile reeftally.py
"$PYTHON" -c 'import reeftally; print("==> import reeftally OK (%s %s)" % (reeftally.TOOL_NAME, reeftally.__version__))'

# 4. Smoke suite (a developer artifact; it is present in the source tree).
if [ -f test_reeftally.py ]; then
    "$PYTHON" -m unittest test_reeftally
    echo "==> smoke suite green"
fi

# 5. Optional convenience: put the `reeftally` console script on PATH. Running
#    `python3 reeftally.py ...` works with or without this step, so an offline
#    machine is not a setup failure.
if [ "${REEFTALLY_SKIP_SCRIPT:-0}" != "1" ]; then
    if "$PYTHON" -m pip install --no-build-isolation -e . >/dev/null 2>&1; then
        echo "==> console script installed: reeftally"
    else
        echo "==> note: the 'reeftally' console script was not installed; use: $PYTHON reeftally.py"
    fi
fi

echo ""
echo "Setup complete - nothing is running."
echo "  help : $PYTHON reeftally.py --help"
echo "  demo : bash demo.sh"
echo "  try  : $PYTHON reeftally.py report examples/hornsby-reef.json"
exit 0
