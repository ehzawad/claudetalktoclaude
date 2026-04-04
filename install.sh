#!/bin/bash
set -e

# Decision Chronicle installer
# Usage: curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash

INSTALL_DIR="$HOME/.chronicle/src"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "Installing Decision Chronicle..."
echo ""

# 1. Check platform
OS="$(uname -s)"
case "$OS" in
    Linux|Darwin) ;;
    *)
        echo "ERROR: Unsupported platform: $OS"
        echo "Decision Chronicle requires macOS or Linux. On Windows, use WSL."
        exit 1
        ;;
esac
echo "Platform: $OS"

# 2. Check dependencies
MISSING=""

if ! command -v git >/dev/null 2>&1; then
    MISSING="$MISSING git"
fi

if ! command -v claude >/dev/null 2>&1; then
    MISSING="$MISSING claude"
fi

# Check for python3 — try multiple names
PYTHON=""
for cmd in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        # Verify it's actually 3.10+
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    MISSING="$MISSING python3.10+"
fi

if [ -n "$MISSING" ]; then
    echo "ERROR: Missing required tools:$MISSING"
    echo ""
    if echo "$MISSING" | grep -q "python"; then
        if [ "$OS" = "Darwin" ]; then
            echo "  Install Python: brew install python"
        else
            echo "  Install Python: sudo apt install python3 python3-venv  (Debian/Ubuntu)"
            echo "                  sudo dnf install python3               (Fedora)"
        fi
    fi
    if echo "$MISSING" | grep -q "git"; then
        if [ "$OS" = "Darwin" ]; then
            echo "  Install git: xcode-select --install"
        else
            echo "  Install git: sudo apt install git"
        fi
    fi
    if echo "$MISSING" | grep -q "claude"; then
        echo "  Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash"
    fi
    exit 1
fi

echo "Python: $PYTHON ($($PYTHON --version 2>&1))"
echo "Claude: $(claude --version 2>/dev/null || echo 'found')"
echo ""

# 3. Clone or update
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    # Reset to match remote — this is an install target, not a dev repo.
    # Local modifications come from `chronicle reload` or editable installs
    # and should not block updates.
    git fetch --quiet origin
    git reset --hard origin/main --quiet
else
    echo "Cloning repository..."
    git clone --quiet https://github.com/ehzawad/claudetalktoclaude.git "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 4. Venv + install
if [ -d "$INSTALL_DIR/.venv" ] && [ -x "$INSTALL_DIR/.venv/bin/pip" ]; then
    echo "Reusing existing Python environment..."
else
    echo "Creating Python environment..."
    "$PYTHON" -m venv .venv 2>/dev/null || {
        echo "ERROR: python3-venv not installed."
        if [ "$OS" = "Darwin" ]; then
            echo "  Run: brew install python"
        else
            echo "  Run: sudo apt install python3-venv"
        fi
        exit 1
    }
fi
.venv/bin/pip install -e . --quiet

# 5. Symlink to PATH
mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_DIR/.venv/bin/chronicle-hook" "$HOME/.local/bin/chronicle-hook"
ln -sf "$INSTALL_DIR/.venv/bin/chronicle" "$HOME/.local/bin/chronicle"

# 6. Check PATH
if ! echo "$PATH" | grep -qF "$HOME/.local/bin"; then
    # Detect shell rc file
    SHELL_RC=""
    case "$(basename "$SHELL")" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac
    # Only append if the export line doesn't already exist in the rc file
    EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if ! grep -qF "$EXPORT_LINE" "$SHELL_RC" 2>/dev/null; then
        echo "$EXPORT_LINE" >> "$SHELL_RC"
        echo "Added ~/.local/bin to PATH in $SHELL_RC"
    fi
    export PATH="$HOME/.local/bin:$PATH"
fi

# 7. Configure hooks
echo "Configuring hooks..."
mkdir -p "$HOME/.claude"
"$INSTALL_DIR/.venv/bin/python3" "$INSTALL_DIR/chronicle/install_hooks.py" "$SETTINGS_FILE"

# 8. Set secure permissions
chmod 700 "$HOME/.chronicle" 2>/dev/null || true

# 9. Verify
echo ""
echo "Verifying installation..."
if command -v chronicle-hook >/dev/null 2>&1 && command -v chronicle >/dev/null 2>&1; then
    echo "  chronicle-hook: $(which chronicle-hook)"
    echo "  chronicle:      $(which chronicle)"
    echo "  version:        $(chronicle --version 2>/dev/null || echo 'unknown')"
    echo ""
    echo "Installation complete!"
    echo ""
    echo "Restart Claude Code to activate hooks."
    echo "After that, everything is automatic."
    echo ""
    echo "Commands:"
    echo "  chronicle query sessions    # check current project"
    echo "  chronicle query timeline    # recent decisions"
    echo "  chronicle batch --workers 5 # process all past sessions"
    echo "  chronicle reload            # after pulling new code"
else
    echo ""
    echo "WARNING: Commands not in current PATH."
    echo "Run this to fix, then try again:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Or restart your terminal."
fi
