#!/usr/bin/env bash
set -euo pipefail

# Slife one-click installer for macOS, Linux, and WSL.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
#
# No prerequisites — the script installs uv if needed, then uses
# ``uv tool install`` to install slife in an isolated environment.
# Python 3.13 is managed automatically by uv.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m' # No Color

SLIFE_REPO="https://github.com/juzcn/slife"
SLIFE_TARBALL="$SLIFE_REPO/archive/refs/heads/main.tar.gz"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║        Slife Installer              ║${NC}"
echo -e "${CYAN}║  Terminal-based AI agent            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Pre-flight summary ──────────────────────────────────────────────
echo "Install method    : uv tool install (isolated environment)"
echo "User data         : ${CYAN}$HOME/.slife/${NC}"
echo "Python            : managed by uv (3.13)"
echo "npx               : auto-install Node.js if needed (required for MCP servers)"
echo "Disk space needed : ~500 MB"
echo ""

# ── 0. Disk space check (before any download) ────────────────────────
if command -v df &>/dev/null; then
    FREE_KB=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
    if [ "$FREE_KB" -gt 0 ] 2>/dev/null && [ "$FREE_KB" -lt 1048576 ]; then
        FREE_MB=$((FREE_KB / 1024))
        echo -e "${RED}Error: only ~${FREE_MB} MB free on $HOME (need >= 1 GB).${NC}"
        echo -e "${YELLOW}Free up space and try again.  Help: $SLIFE_REPO${NC}"
        exit 1
    fi
fi

# ── 1. Ensure uv is available ──────────────────────────────────────
echo -e "${YELLOW}[1/5] Ensuring uv is available…${NC}"
if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}  Installing uv…${NC}"
    curl --progress-bar -Lf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo -e "${GREEN}  ✓${NC} uv $(uv --version 2>&1)"

# ── 2. Ensure npx (Node.js) is available ─────────────────────────────
echo -e "${YELLOW}[2/5] Ensuring npx (Node.js) is available…${NC}"
HAVE_NPX=false
if command -v npx &>/dev/null; then
    echo -e "${GREEN}  ✓${NC} npx v$(npx --version 2>&1)"
    HAVE_NPX=true
fi

if [ "$HAVE_NPX" = false ]; then
    echo -e "${YELLOW}  npx not found, installing Node.js…${NC}"
    # Try official repos (safe — no curl-to-shell).  Do NOT pipe
    # third-party scripts directly into sudo bash (security risk).
    if command -v apt-get &>/dev/null; then
        echo -e "${YELLOW}  Installing Node.js via apt…${NC}"
        sudo apt-get update -qq && sudo apt-get install -y nodejs npm 2>/dev/null || true
    elif command -v brew &>/dev/null; then
        echo -e "${YELLOW}  Installing Node.js via Homebrew…${NC}"
        brew install node 2>/dev/null || true
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y nodejs npm 2>/dev/null || true
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm nodejs npm 2>/dev/null || true
    fi
    # Re-check after install attempt
    if command -v npx &>/dev/null; then
        echo -e "${GREEN}  ✓${NC} npx v$(npx --version 2>&1)"
        HAVE_NPX=true
    fi
    if [ "$HAVE_NPX" = false ]; then
        echo -e "${RED}  ┌─────────────────────────────────────────────────────┐${NC}"
        echo -e "${RED}  │  WARNING: npx not available.                       │${NC}"
        echo -e "${RED}  │                                                     │${NC}"
        echo -e "${RED}  │  These MCP servers require npx and will NOT work:    │${NC}"
        echo -e "${RED}  │    file-search, serper, tavily-mcp, github,          │${NC}"
        echo -e "${RED}  │    amap-maps, filesystem                             │${NC}"
        echo -e "${RED}  │                                                     │${NC}"
        echo -e "${RED}  │  Install Node.js LTS from https://nodejs.org         │${NC}"
        echo -e "${RED}  │  then re-run this installer.                         │${NC}"
        echo -e "${RED}  └─────────────────────────────────────────────────────┘${NC}"
        echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
        exit 1
    fi
fi

# ── Optional: Mosquitto MQTT broker (for A2A multi-agent mesh) ────────
echo -e "${YELLOW}[optional] Checking Mosquitto (MQTT broker for multi-agent mesh)…${NC}"
if command -v mosquitto &>/dev/null; then
    echo -e "${GREEN}  ✓${NC} mosquitto found"
else
    echo -e "${YELLOW}  Mosquitto not found.${NC}"
    echo -e "${GRAY}  Required for: A2A multi-agent mesh communication${NC}"
    echo -e "${GRAY}  Without it:  slife works normally, just without P2P agent features${NC}"
    if [ -t 0 ]; then
        read -p "  Install Mosquitto? (y/n, default: n): " choice
    else
        choice="n"
    fi
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
        if command -v apt-get &>/dev/null; then
            echo -e "${YELLOW}  Installing Mosquitto via apt…${NC}"
            sudo apt-get install -y mosquitto mosquitto-clients 2>/dev/null || true
        elif command -v brew &>/dev/null; then
            echo -e "${YELLOW}  Installing Mosquitto via Homebrew…${NC}"
            brew install mosquitto 2>/dev/null || true
        elif command -v dnf &>/dev/null; then
            echo -e "${YELLOW}  Installing Mosquitto via dnf…${NC}"
            sudo dnf install -y mosquitto 2>/dev/null || true
        elif command -v pacman &>/dev/null; then
            echo -e "${YELLOW}  Installing Mosquitto via pacman…${NC}"
            sudo pacman -S --noconfirm mosquitto 2>/dev/null || true
        else
            echo -e "${YELLOW}  No supported package manager found.${NC}"
            echo -e "${YELLOW}  Install manually: https://mosquitto.org/download/${NC}"
        fi
        # Re-check
        if command -v mosquitto &>/dev/null; then
            echo -e "${GREEN}  ✓${NC} Mosquitto installed"
            echo -e "${CYAN}  To start Mosquitto:${NC}"
            echo "    mosquitto -d"
            echo -e "${CYAN}  Or as a system service:${NC}"
            echo "    sudo systemctl enable --now mosquitto   # systemd (Linux)"
            echo "    brew services start mosquitto           # Homebrew (macOS)"
        fi
    else
        echo -e "${GRAY}  Skipped. Install later with your package manager.${NC}"
    fi
fi

# ── 3. Download and verify slife ─────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/5] Downloading slife…${NC}"
curl --progress-bar -fL "$SLIFE_TARBALL" -o "$TMP_DIR/slife.tar.gz"

# Print SHA256 so users can verify integrity if desired.
echo -e "  SHA256: ${GRAY}$(sha256sum "$TMP_DIR/slife.tar.gz" 2>/dev/null || shasum -a 256 "$TMP_DIR/slife.tar.gz" 2>/dev/null || echo '(sha256sum not available)')${NC}"

tar xzf "$TMP_DIR/slife.tar.gz" -C "$TMP_DIR"

# Read version from pyproject.toml.
VERSION="unknown"
PYPROJECT="$TMP_DIR/slife-main/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
    EXTRACTED_VERSION=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PYPROJECT" 2>/dev/null || echo "")
    if [ -n "$EXTRACTED_VERSION" ]; then
        VERSION="$EXTRACTED_VERSION"
    fi
fi

# ── 4. Install slife with uv tool install ────────────────────────────
# uv tool install creates an isolated venv, installs slife + credstore
# (workspace member), and places the executables in ~/.local/bin.
# Python 3.13 is managed automatically by uv.
# User data (~/.slife/) is never touched.
echo -e "${YELLOW}[4/5] Installing slife v${VERSION}…${NC}"

# Detect previously installed optional packages so we can preserve
# them across reinstall.  Without this, "uv tool uninstall" + "uv tool
# install" silently drops llama-cpp-python / sentence-transformers,
# which live in optional-dependencies and are not installed by default.
#
# Capture all user-installed packages from the old venv (including
# extras and manually pip-installed packages) so we can re-add them
# after the fresh install.
# Save outside TMP_DIR so the file survives cleanup.
PRESERVED_REQS="${TMPDIR:-/tmp}/slife-preserved-requirements.txt"
:> "$PRESERVED_REQS"
SLIFE_LINE=$(uv tool list --show-paths 2>/dev/null | grep "slife v" || true)
if [ -n "$SLIFE_LINE" ]; then
    SLIFE_VENV=$(echo "$SLIFE_LINE" | sed -n 's/.*(\(.*\)).*/\1/p')
    if [ -n "$SLIFE_VENV" ] && [ -d "$SLIFE_VENV" ]; then
        OLD_PYTHON="$SLIFE_VENV/bin/python"
        if [ -x "$OLD_PYTHON" ]; then
            echo -e "  ${GRAY}Capturing installed packages from previous installation…${NC}"
            # Save full freeze (with versions) for display.
            PRESERVED_FULL="${TMPDIR:-/tmp}/slife-preserved-full.txt"
            uv pip freeze --python "$OLD_PYTHON" 2>/dev/null \
                | grep -v '^-e ' \
                | grep -vE '^(slife|credstore)[ @=]' > "$PRESERVED_FULL" || true
            # Save name-only list for diff.
            sed 's/ @ .*//; s/==.*//' "$PRESERVED_FULL" > "$PRESERVED_REQS"
            _count=$(wc -l < "$PRESERVED_REQS" 2>/dev/null || echo 0)
            if [ "$_count" -gt 0 ]; then
                echo -e "  ${GRAY}Detected $_count packages to preserve${NC}"
            fi
        fi
    fi
fi

# Clean up any previous broken installation first.
if uv tool list 2>/dev/null | grep -qF "slife"; then
    echo -e "${YELLOW}  Removing previous slife installation…${NC}"
    uv tool uninstall slife 2>/dev/null || true
fi

# Clean up old venv artifacts if migrating from a previous install
# that placed the venv inside ~/.slife/.  User data is preserved.
if [ -f "$HOME/.slife/pyvenv.cfg" ]; then
    echo -e "${YELLOW}  Cleaning up old venv-based installation…${NC}"
    for artifact in bin lib include pyvenv.cfg Scripts Lib Include; do
        [ -e "$HOME/.slife/$artifact" ] && rm -rf "$HOME/.slife/$artifact"
    done
fi

TOOL_INSTALL_LOG="$TMP_DIR/tool-install.log"
# Capture output to a log file first, then display it.  This avoids any
# pipefail / bash-version edge cases with ``set -o pipefail`` + ``tee``.
echo -e "  ${GRAY}(output captured to $TOOL_INSTALL_LOG)${NC}"
set +eo pipefail
uv tool install --from "$TMP_DIR/slife-main" --python 3.13 slife > "$TOOL_INSTALL_LOG" 2>&1
INSTALL_EXIT=$?
set -eo pipefail
cat "$TOOL_INSTALL_LOG"
if [ $INSTALL_EXIT -ne 0 ]; then
    echo -e "${RED}Error: slife installation failed (exit $INSTALL_EXIT).${NC}"
    echo -e "${YELLOW}Last lines of install log:${NC}"
    tail -n 20 "$TOOL_INSTALL_LOG"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
fi

# Re-add preserved packages into the new tool venv.
if [ -s "$PRESERVED_REQS" ]; then
    NEW_LINE=$(uv tool list --show-paths 2>/dev/null | grep "slife v")
    NEW_VENV=$(echo "$NEW_LINE" | sed -n 's/.*(\(.*\)).*/\1/p')
    if [ -n "$NEW_VENV" ] && [ -d "$NEW_VENV" ]; then
        NEW_PYTHON="$NEW_VENV/bin/python"

        # Read extra-index-url from pyproject.toml for pre-built wheels.
        # Handles both single-line value and multi-line array formats.
        PYPROJECT="$TMP_DIR/slife-main/pyproject.toml"
        EXTRA_INDEX_ARGS=""
        if [ -f "$PYPROJECT" ]; then
            _url=$(grep -A2 'extra-index-url' "$PYPROJECT" 2>/dev/null | grep -o 'https\?://[^"]*' | head -1 || true)
            [ -n "$_url" ] && EXTRA_INDEX_ARGS="--extra-index-url $_url"
        fi

        # Diff old freeze against new venv — only re-add packages not
        # already in the base install (avoids conflicts with transitive deps).
        EXTRA_REQS="$TMPDIR/slife-extra-requirements.txt"
        uv pip freeze --python "$NEW_PYTHON" 2>/dev/null | sed 's/==.*//' | sort > "$TMP_DIR/new-freeze.txt"
        sort "$PRESERVED_REQS" | comm -23 - "$TMP_DIR/new-freeze.txt" > "$EXTRA_REQS"
        _extra_count=$(wc -l < "$EXTRA_REQS" 2>/dev/null || echo 0)

        if [ "$_extra_count" -eq 0 ]; then
            echo -e "  ${GRAY}All packages already present — nothing to re-add${NC}"
            PRESERVE_OK=1
        else
            echo -e "${YELLOW}  Re-adding $_extra_count extra packages:${NC}"
            # Show versions from full freeze.
            while IFS= read -r _pkg; do
                _ver=$(grep "^$_pkg[ @=]" "$PRESERVED_FULL" 2>/dev/null | head -1 || echo "$_pkg")
                echo -e "    ${GRAY}$_ver${NC}"
            done < "$EXTRA_REQS"
            # shellcheck disable=SC2086
            if uv pip install --python "$NEW_PYTHON" $EXTRA_INDEX_ARGS -r "$EXTRA_REQS" >> "$TOOL_INSTALL_LOG" 2>&1; then
                PRESERVE_OK=1
                echo -e "  ${GREEN}  ✓${NC} $_extra_count packages restored"
            else
                PRESERVE_OK=0
                echo -e "  ${YELLOW}  ⚠ failed — see log or run: uv pip install -r $EXTRA_REQS${NC}"
                echo -e "  ${GRAY}  Error details:${NC}"
                tail -15 "$TOOL_INSTALL_LOG" | while IFS= read -r _line; do echo -e "    ${GRAY}$_line${NC}"; done
            fi
        fi
    fi
fi

# ── 5. Clean up previous installation artifacts ──────────────────────
echo -e "${YELLOW}[5/5] Cleaning up previous installation artifacts…${NC}"

# Ensure ~/.local/bin is on PATH (uv puts tool executables here,
# and Step 1 only added it to the current session).
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" "$HOME/.config/fish/config.fish"; do
    if [ -f "$rc" ]; then
        if ! grep -qF "$HOME/.local/bin" "$rc" 2>/dev/null; then
            if echo "$rc" | grep -q fish; then
                echo "fish_add_path $HOME/.local/bin" >> "$rc"
            else
                echo "export PATH=\"$HOME/.local/bin:\$PATH\"" >> "$rc"
            fi
        fi
    fi
done
export PATH="$HOME/.local/bin:$PATH"

echo -e "${GREEN}  ✓${NC} slife + credstore commands ready"

# Verify the binary is actually reachable and show its location.
if command -v slife &>/dev/null; then
    SLIFE_PATH="$(command -v slife)"
    echo -e "  ${GREEN}  slife${NC} → ${GRAY}$SLIFE_PATH${NC}"
else
    echo -e "  ${RED}  ⚠ slife binary not found on PATH${NC}"
fi

# ── 6. Done ───────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Slife v${VERSION} installed successfully! 🎉  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# When piped to bash, the exports above only affect the script's subshell —
# not the user's interactive shell.  Remind them to refresh their PATH.
NEEDS_SHELL_REFRESH=true
if command -v slife &>/dev/null; then
    NEEDS_SHELL_REFRESH=false
fi

if [ "$NEEDS_SHELL_REFRESH" = true ]; then
    echo -e "${YELLOW}┌─────────────────────────────────────────────────────┐${NC}"
    echo -e "${YELLOW}│  IMPORTANT: slife is installed but not on your      │${NC}"
    echo -e "${YELLOW}│  current PATH (pipe-to-bash runs in a subshell).    │${NC}"
    echo -e "${YELLOW}│  To use it, run:                                    │${NC}"
    echo -e "${YELLOW}│                                                     │${NC}"
    echo -e "${YELLOW}│    source \"\$HOME/.local/bin/env\"                     │${NC}"
    echo -e "${YELLOW}│                                                     │${NC}"
    echo -e "${YELLOW}│    Or simply open a new terminal.                   │${NC}"
    echo -e "${YELLOW}└─────────────────────────────────────────────────────┘${NC}"
    echo ""
fi

echo -e "${CYAN}Get started:${NC}"
echo "  credstore set-password              # set up encrypted backup (first time)"
echo "  credstore set DEEPSEEK_API_KEY       # store your API key"
echo "  slife                                # launch the TUI"
echo ""
if [ -n "${EXTRA_REQS:-}" ] && [ -s "$EXTRA_REQS" ]; then
    if [ "${PRESERVE_OK:-0}" = "1" ]; then
        echo -e "${CYAN}Preserved packages:${NC}"
        _count=$(wc -l < "$EXTRA_REQS" 2>/dev/null || echo 0)
        echo -e "${GREEN}  ✓${NC} $_count extra packages restored from previous install"
    else
        echo -e "${YELLOW}Failed to preserve packages — run manually:${NC}"
        echo -e "${YELLOW}  uv pip install -r $EXTRA_REQS${NC}"
    fi
fi
echo -e "${CYAN}Optional extras:${NC}"
echo "  # Local GGUF embeddings (offline, ~30 MB):"
echo "  uv tool install --with \"slife[gguf]\" slife"
echo "  # HuggingFace transformer embeddings (~2 GB):"
echo "  uv tool install --with \"slife[transformer]\" slife"
echo ""
echo -e "${CYAN}More info:${NC} $SLIFE_REPO"
