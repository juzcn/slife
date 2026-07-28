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
# We check how each package was installed:
#   - direct_url.json present → installed from a specific URL → replay URL
#   - no direct_url.json      → installed from an index    → uv pip install <name>
PRESERVED_PKGS=""      # space-separated: "name" or "name|url"
SLIFE_LINE=$(uv tool list --show-paths 2>/dev/null | grep "slife v")
if [ -n "$SLIFE_LINE" ]; then
    SLIFE_VENV=$(echo "$SLIFE_LINE" | sed -n 's/.*(\(.*\)).*/\1/p')
    if [ -n "$SLIFE_VENV" ] && [ -d "$SLIFE_VENV" ]; then
        # Find site-packages (Python version may vary).
        SITE_PKGS=$(echo "$SLIFE_VENV"/lib/python*/site-packages 2>/dev/null | head -1)

        _detect_pkg() {
            _import="$1" _name="$2"
            if [ -d "$SITE_PKGS/$_import" ] 2>/dev/null; then
                _url=""
                _di=$(echo "$SITE_PKGS/${_import}-"*.dist-info 2>/dev/null | head -1)
                if [ -n "$_di" ] && [ -f "$_di/direct_url.json" ]; then
                    _url=$(python3 -c "import json; print(json.load(open('$_di/direct_url.json'))['url'])" 2>/dev/null || true)
                fi
                if [ -n "$_url" ]; then
                    PRESERVED_PKGS="$PRESERVED_PKGS ${_name}|${_url}"
                    echo -e "  ${GRAY}Detected: $_name (from URL, will preserve)${NC}"
                else
                    PRESERVED_PKGS="$PRESERVED_PKGS $_name"
                    echo -e "  ${GRAY}Detected: $_name (will preserve)${NC}"
                fi
            fi
        }
        _detect_pkg "llama_cpp" "llama-cpp-python"
        _detect_pkg "sentence_transformers" "sentence-transformers"
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
uv tool install --from "$TMP_DIR/slife-main" --python 3.13 slife > "$TOOL_INSTALL_LOG" 2>&1 || {
    echo -e "${RED}Error: slife installation failed.${NC}"
    echo -e "${YELLOW}Last lines of install log:${NC}"
    tail -n 20 "$TOOL_INSTALL_LOG"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
}

# Re-add preserved packages into the new tool venv.
if [ -n "$PRESERVED_PKGS" ]; then
    NEW_LINE=$(uv tool list --show-paths 2>/dev/null | grep "slife v")
    NEW_VENV=$(echo "$NEW_LINE" | sed -n 's/.*(\(.*\)).*/\1/p')
    if [ -n "$NEW_VENV" ] && [ -d "$NEW_VENV" ]; then
        NEW_PYTHON="$NEW_VENV/bin/python"

        # Read extra-index-url from the project's pyproject.toml so that
        # packages like llama-cpp-python can resolve pre-built wheels.
        EXTRA_INDEX_ARGS=""
        PYPROJECT="$TMP_DIR/slife-main/pyproject.toml"
        if [ -f "$PYPROJECT" ]; then
            _in_tool_uv=""
            while IFS= read -r _line; do
                case "$_line" in
                    '['tool.uv']') _in_tool_uv=1 ;;
                    '['*']') [ -n "$_in_tool_uv" ] && break ;;
                esac
                if [ -n "$_in_tool_uv" ]; then
                    _url=$(echo "$_line" | sed -n 's/.*extra-index-url[[:space:]]*=[[:space:]]*\["\([^"]*\)"\].*/\1/p')
                    if [ -n "$_url" ]; then
                        _url=$(echo "$_url" | sed "s/^\[//; s/\]$//; s/\"//g; s/,$//; s/^[[:space:]]*//; s/[[:space:]]*$//")
                        [ -n "$_url" ] && EXTRA_INDEX_ARGS="--extra-index-url $_url"
                        break
                    fi
                fi
            done < "$PYPROJECT"
        fi

        echo -e "${YELLOW}  Re-adding preserved packages…${NC}"
        _ok_count=0
        _fail_count=0
        for entry in $PRESERVED_PKGS; do
            _name="${entry%%|*}"
            _url="${entry#*|}"
            # shellcheck disable=SC2086
            if [ "$_url" != "$_name" ]; then
                uv pip install --python "$NEW_PYTHON" $EXTRA_INDEX_ARGS "$_url" >> "$TOOL_INSTALL_LOG" 2>&1
            else
                uv pip install --python "$NEW_PYTHON" $EXTRA_INDEX_ARGS "$_name" >> "$TOOL_INSTALL_LOG" 2>&1
            fi
            if [ $? -eq 0 ]; then
                echo -e "  ${GREEN}  ✓${NC} $_name"
                _ok_count=$((_ok_count + 1))
            else
                echo -e "  ${YELLOW}  ⚠ failed to re-add $_name — run: uv pip install $_name${NC}"
                _fail_count=$((_fail_count + 1))
            fi
        done
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

# ── 6. Done ───────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Slife v${VERSION} installed successfully! 🎉  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}Get started:${NC}"
echo "  credstore set-password              # set up encrypted backup (first time)"
echo "  credstore set DEEPSEEK_API_KEY       # store your API key"
echo "  slife                                # launch the TUI"
echo ""
if [ -n "$PRESERVED_PKGS" ]; then
    echo -e "${CYAN}Preserved packages:${NC}"
    for entry in $PRESERVED_PKGS; do
        _name="${entry%%|*}"
        echo -e "${GREEN}  ✓${NC} $_name (auto-detected from previous install)"
    done
fi
echo -e "${CYAN}Optional extras:${NC}"
echo "  uv tool install --with \"slife[gguf]\" slife    # local GGUF models"
echo "  uv tool install --with \"slife[transformer]\" slife  # HuggingFace embeddings (~2 GB)"
echo ""
echo -e "${CYAN}More info:${NC} $SLIFE_REPO"
