#!/bin/bash
set -euo pipefail

# Decision Chronicle installer — downloads a prebuilt binary release.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash
#
# Environment overrides (for testing / pinning):
#   CHRONICLE_VERSION  — git tag, e.g. vX.Y.Z. Default: latest release.
#   CHRONICLE_BASE_URL — override download host (e.g. local mirror).
#   CHRONICLE_HOME     — data + runtime root. Default: $HOME/.chronicle.

REPO_SLUG="ehzawad/claudetalktoclaude"
CHRONICLE_HOME="${CHRONICLE_HOME:-$HOME/.chronicle}"
BIN_DIR="$HOME/.local/bin"
RUNTIME_DIR="$CHRONICLE_HOME/runtime"
VERSION="${CHRONICLE_VERSION:-latest}"
BASE_URL="${CHRONICLE_BASE_URL:-https://github.com/$REPO_SLUG/releases}"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "Installing Decision Chronicle..."
echo ""

# -----------------------------------------------------------------------------
# 1. Detect platform
# -----------------------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
    Darwin/arm64)       TARGET="darwin-arm64" ;;
    Linux/x86_64)       TARGET="linux-x86_64" ;;
    Darwin/x86_64)
        echo "ERROR: macOS Intel is not a prebuilt target yet."
        echo "  Build locally: git clone git@github.com:$REPO_SLUG.git && cd claudetalktoclaude && pip install pyinstaller -e . && pyinstaller --name chronicle --onedir --clean --noupx chronicle/_entrypoint.py"
        exit 1
        ;;
    Linux/aarch64|Linux/arm64)
        echo "ERROR: Linux arm64 is not a prebuilt target yet."
        echo "  File an issue or build locally (same recipe as above)."
        exit 1
        ;;
    *)
        echo "ERROR: unsupported platform $OS/$ARCH"
        exit 1
        ;;
esac
echo "Platform: $TARGET"

# -----------------------------------------------------------------------------
# 2. Check dependencies (just curl/tar/claude — no Python, no git needed)
# -----------------------------------------------------------------------------
MISSING=""
for bin in curl tar; do
    command -v "$bin" >/dev/null 2>&1 || MISSING="$MISSING $bin"
done
CLAUDE_FOUND=""
if command -v claude >/dev/null 2>&1; then
    CLAUDE_FOUND="$(command -v claude)"
else
    for d in "$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
        if [ -x "$d/claude" ]; then
            CLAUDE_FOUND="$d/claude"
            break
        fi
    done
fi
[ -z "$CLAUDE_FOUND" ] && MISSING="$MISSING claude"

if [ -n "$MISSING" ]; then
    echo "ERROR: Missing required tools:$MISSING"
    echo ""
    if echo "$MISSING" | grep -q claude; then
        echo "  Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"
    fi
    exit 1
fi
echo "Claude:   $CLAUDE_FOUND"

# -----------------------------------------------------------------------------
# 3. Resolve download URLs
# -----------------------------------------------------------------------------
if [ "$VERSION" = "latest" ]; then
    # GitHub's /releases/latest/download/<asset> follows the redirect to the
    # newest tagged release — no REST API call, no jq, no rate limit.
    ASSET_URL="$BASE_URL/latest/download/chronicle-$TARGET.tar.gz"
    SHA_URL="$BASE_URL/latest/download/chronicle-$TARGET.tar.gz.sha256"
else
    ASSET_URL="$BASE_URL/download/$VERSION/chronicle-$TARGET.tar.gz"
    SHA_URL="$BASE_URL/download/$VERSION/chronicle-$TARGET.tar.gz.sha256"
fi
echo "Asset:    $ASSET_URL"

# -----------------------------------------------------------------------------
# 4. Download + verify + extract
# -----------------------------------------------------------------------------
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
cd "$TMPDIR"

echo "Downloading..."
curl -fL --progress-bar -o chronicle.tar.gz "$ASSET_URL"
curl -fsSL -o chronicle.tar.gz.sha256 "$SHA_URL"

echo "Verifying SHA256..."
EXPECTED=$(awk '{print $1}' chronicle.tar.gz.sha256)
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum chronicle.tar.gz | awk '{print $1}')
else
    ACTUAL=$(shasum -a 256 chronicle.tar.gz | awk '{print $1}')
fi
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "ERROR: SHA256 mismatch"
    echo "  expected: $EXPECTED"
    echo "  actual:   $ACTUAL"
    exit 1
fi
echo "SHA256 ok: $ACTUAL"

echo "Extracting..."
tar -xzf chronicle.tar.gz

# -----------------------------------------------------------------------------
# 5. Clean up legacy install layouts
# -----------------------------------------------------------------------------
# Stop the daemon if it's running, so we can safely replace the binary.
DAEMON_WAS_RUNNING=0
if [ -f "$CHRONICLE_HOME/daemon.pid" ]; then
    DAEMON_PID=$(cat "$CHRONICLE_HOME/daemon.pid" 2>/dev/null || echo "")
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        DAEMON_CMD="$(ps -p "$DAEMON_PID" -o command= 2>/dev/null || true)"
        if echo "$DAEMON_CMD" | grep -q "chronicle"; then
            DAEMON_WAS_RUNNING=1
            kill -TERM "$DAEMON_PID" 2>/dev/null || true
            # Give launchd/systemd a moment to notice before we overwrite files.
            sleep 1
        else
            echo "Ignoring stale daemon.pid ($DAEMON_PID does not look like chronicle)."
        fi
    fi
fi

# Remove the old source-tree install (venv + shell wrappers + git clone).
# The binary doesn't need any of it. Keep user data under $CHRONICLE_HOME,
# just nuke the managed .src dir if it's there.
if [ -d "$CHRONICLE_HOME/src" ]; then
    echo "Removing legacy source-tree install at $CHRONICLE_HOME/src..."
    rm -rf "$CHRONICLE_HOME/src"
fi

# -----------------------------------------------------------------------------
# 6. Validate the new runtime before swapping it live
# -----------------------------------------------------------------------------
mkdir -p "$BIN_DIR" "$CHRONICLE_HOME"
NEW_RUNTIME="$CHRONICLE_HOME/runtime.new"
rm -rf "$NEW_RUNTIME"
mv "chronicle-$TARGET" "$NEW_RUNTIME"

# macOS: strip quarantine. curl-downloaded files rarely carry quarantine, but
# tar can import it from individual entries, and some corporate MDM policies
# attach it. Clearing it here avoids Gatekeeper killing every binary launch.
if [ "$OS" = "Darwin" ]; then
    xattr -dr com.apple.quarantine "$NEW_RUNTIME" 2>/dev/null || true
fi

echo "Validating hook installation..."
mkdir -p "$HOME/.claude"
"$NEW_RUNTIME/chronicle" install-hooks "$SETTINGS_FILE"

# -----------------------------------------------------------------------------
# 7. Install runtime + symlinks
# -----------------------------------------------------------------------------
# Atomic swap: validate first, then rename. Prevents half-written runtime
# dir from being live if hook installation fails.
if [ -d "$RUNTIME_DIR" ]; then
    OLD_RUNTIME="$CHRONICLE_HOME/runtime.old"
    rm -rf "$OLD_RUNTIME"
    mv "$RUNTIME_DIR" "$OLD_RUNTIME"
fi
mv "$NEW_RUNTIME" "$RUNTIME_DIR"
rm -rf "$CHRONICLE_HOME/runtime.old"

# Old symlinks / wrapper scripts from earlier layouts.
rm -f "$BIN_DIR/chronicle" "$BIN_DIR/chronicle-hook"
# Relative symlinks so the install layout stays portable if $HOME moves.
ln -sf "$RUNTIME_DIR/chronicle" "$BIN_DIR/chronicle"
ln -sf "chronicle" "$BIN_DIR/chronicle-hook"

# -----------------------------------------------------------------------------
# 8. PATH check
# -----------------------------------------------------------------------------
if ! echo ":$PATH:" | grep -qF ":$BIN_DIR:"; then
    SHELL_RC=""
    case "$(basename "${SHELL:-}")" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac
    EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if ! grep -qF "$EXPORT_LINE" "$SHELL_RC" 2>/dev/null; then
        echo "$EXPORT_LINE" >> "$SHELL_RC"
        echo "Added ~/.local/bin to PATH in $SHELL_RC"
    fi
    export PATH="$BIN_DIR:$PATH"
fi

# -----------------------------------------------------------------------------
# 9. Tighten data dir perms + restart daemon if needed
# -----------------------------------------------------------------------------
chmod 700 "$CHRONICLE_HOME" 2>/dev/null || true

EFFECTIVE_MODE=$("$BIN_DIR/chronicle" doctor 2>/dev/null | awk '/^mode:/ {print $2}' || true)
[ -z "$EFFECTIVE_MODE" ] && EFFECTIVE_MODE="foreground"

if [ "$EFFECTIVE_MODE" = "background" ]; then
    if [ "$OS" = "Darwin" ]; then
        if launchctl print "gui/$(id -u)/com.chronicle.daemon" >/dev/null 2>&1; then
            launchctl kickstart -k "gui/$(id -u)/com.chronicle.daemon" >/dev/null 2>&1 \
                && echo "Kickstarted launchd daemon (new binary active)." \
                || echo "  (launchctl kickstart failed; daemon will pick up new binary on next restart)"
        fi
    elif [ "$OS" = "Linux" ]; then
        if systemctl --user is-active --quiet chronicle-daemon.service 2>/dev/null; then
            systemctl --user restart chronicle-daemon.service \
                && echo "Restarted systemd daemon (new binary active)." \
                || echo "  (systemctl restart failed; daemon will pick up new binary on next restart)"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 10. Verify + summary
# -----------------------------------------------------------------------------
echo ""
echo "Installed:"
echo "  $BIN_DIR/chronicle      -> $RUNTIME_DIR/chronicle"
echo "  $BIN_DIR/chronicle-hook -> chronicle"
echo "  runtime:                 $RUNTIME_DIR  ($(du -sh "$RUNTIME_DIR" 2>/dev/null | awk '{print $1}'))"
echo "  version:                 $("$BIN_DIR/chronicle" --version 2>/dev/null || echo 'unknown')"
echo "  mode:                    $EFFECTIVE_MODE"
echo ""
echo "Installation complete!"
echo ""
echo "Restart Claude Code so the hooks take effect."
echo ""
echo "Other useful commands:"
echo "  chronicle doctor            # diagnose config, daemon status, drift"
echo "  chronicle update            # fetch and install the latest release"
echo "  chronicle install-daemon    # switch to background summarization mode"
echo "  chronicle query timeline    # recent sessions across all projects"
