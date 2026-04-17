#!/bin/sh
# chronicle CLI self-healing wrapper.
#
# Same rebuild-on-dead-venv logic as wrapper-chronicle-hook.sh, but for the
# interactive CLI, failures surface to the user (stderr + non-zero exit)
# instead of being swallowed.
set -u

CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/.chronicle}"
SRC="$CHRONICLE_HOME/src"
VENV="$SRC/.venv"
LOCK="$VENV.rebuild.lock"
LOG="$CHRONICLE_HOME/install-errors.log"
WRAPPER_NAME="chronicle"

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
    echo "chronicle: venv is broken (base Python likely upgraded); rebuilding..." >&2
    rebuild_venv || {
        echo "chronicle: venv rebuild failed. See $LOG for details." >&2
        exit 1
    }
    echo "chronicle: rebuild complete." >&2
fi

exec "$VENV/bin/python3" -m chronicle "$@"
