#!/bin/sh
# chronicle-hook self-healing wrapper.
#
# Runs on every Claude Code SessionStart / Stop / UserPromptSubmit / SessionEnd.
# The base Python interpreter that .venv was built against can disappear (e.g.
# system upgrade 3.14.3 -> 3.14.4 removes /opt/python/3.14.3/...), which kills
# every shebang inside .venv. This wrapper probes the venv, auto-rebuilds if
# dead, and exec's the real entry point. Written in /bin/sh so it can never
# itself be broken by a Python upgrade.
#
# Hook-safety contract: on unrecoverable failure, log and exit 0. Never block
# Claude Code.
set -u

CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/.chronicle}"
SRC="$CHRONICLE_HOME/src"
VENV="$SRC/.venv"
LOCK="$VENV.rebuild.lock"
LOG="$CHRONICLE_HOME/install-errors.log"
WRAPPER_NAME="chronicle-hook"

probe() {
    [ -e "$VENV/bin/python3" ] && "$VENV/bin/python3" -c "import chronicle" >/dev/null 2>&1
}

do_rebuild() {
    probe && return 0
    PY=""
    for cmd in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$cmd" >/dev/null 2>&1 || continue
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
        maj=${ver%.*}
        min=${ver#*.}
        if [ "$maj" -ge 3 ] 2>/dev/null && [ "$min" -ge 10 ] 2>/dev/null; then
            PY="$cmd"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "$(date): $WRAPPER_NAME: no python3.10+ on PATH; cannot rebuild venv" >> "$LOG"
        return 1
    fi
    rm -rf "$VENV"
    "$PY" -m venv "$VENV" >> "$LOG" 2>&1 || return 1
    "$VENV/bin/pip" install -e "$SRC" --quiet >> "$LOG" 2>&1 || return 1
    return 0
}

rebuild_venv() {
    mkdir -p "$CHRONICLE_HOME"
    waited=0
    while ! mkdir "$LOCK" 2>/dev/null; do
        probe && return 0
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge 60 ]; then
            echo "$(date): $WRAPPER_NAME: rebuild lock held too long" >> "$LOG"
            return 1
        fi
    done
    do_rebuild
    ret=$?
    rmdir "$LOCK" 2>/dev/null || true
    return $ret
}

if ! probe; then
    rebuild_venv || {
        echo "$(date): $WRAPPER_NAME: venv rebuild failed; skipping hook run" >> "$LOG"
        exit 0
    }
fi

exec "$VENV/bin/python3" -m chronicle.hook "$@"
