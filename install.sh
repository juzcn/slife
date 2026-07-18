#!/usr/bin/env bash
set -euo pipefail

# Slife one-click installer for macOS, Linux, and WSL.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
#
# No prerequisites — the script installs Python 3.13 and uv if needed.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

SLIFE_TARBALL="https://github.com/juzcn/slife/archive/refs/heads/main.tar.gz"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║        Slife Installer              ║${NC}"
echo -e "${CYAN}║  Terminal-based AI agent            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ── 1. Ensure uv is available ───────────────────────────────────────
# uv's installer is a standalone binary — no Python required.
if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}Installing uv (package manager)…${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo -e "${GREEN}✓${NC} uv $(uv --version 2>&1)"

# ── 2. Ensure Python >= 3.13 is available ───────────────────────────
echo -n "Checking for Python >= 3.13… "
PYTHON=""
for candidate in python3.13 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null || echo "0.0")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -gt 3 ] || ([ "$major" -eq 3 ] && [ "$minor" -ge 13 ]); then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    # Not on PATH — check if uv already manages a Python 3.13
    UV_PYTHON="$(uv python find 3.13 2>/dev/null || echo "")"
    if [ -n "$UV_PYTHON" ]; then
        echo -e "${GREEN}found (uv-managed)${NC}"
        PYTHON="$UV_PYTHON"
    else
        echo -e "${YELLOW}not found${NC}"
        echo -e "${YELLOW}Installing Python 3.13 via uv…${NC}"
        uv python install 3.13
        PYTHON="$(uv python find 3.13 2>/dev/null || echo "")"
        if [ -z "$PYTHON" ]; then
            echo -e "${RED}Error: could not install Python 3.13.${NC}"
            echo "Install manually from https://python.org/downloads/"
            exit 1
        fi
        echo -e "${GREEN}✓${NC} Installed at: ${CYAN}$PYTHON${NC}"
    fi
else
    echo -e "${GREEN}found${NC}"
fi
echo -e "  Selected: ${CYAN}$PYTHON${NC} ($(uv run --python 3.13 python --version 2>&1))"

# ── 3. Download and install slife ────────────────────────────────────
echo ""
echo "Downloading slife…"
curl -fsSL "$SLIFE_TARBALL" -o "$TMP_DIR/slife.tar.gz"
tar xzf "$TMP_DIR/slife.tar.gz" -C "$TMP_DIR"

echo "Installing slife…"
uv tool install --python 3.13 "$TMP_DIR/slife-main"

# ── 4. Done ──────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Slife installed successfully! 🎉           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}Quick start:${NC}"
echo "  credstore set-password              # set up encrypted backup (first time)"
echo "  credstore set DEEPSEEK_API_KEY       # store your API key"
echo "  slife                                # launch the TUI"
echo ""
echo -e "${CYAN}Optional extras:${NC}"
echo "  uv tool install --python 3.13 'slife[embeddings]' --reinstall"
echo "  uv tool install --python 3.13 'slife[mqtt]' --reinstall"
echo ""
echo -e "${CYAN}More info:${NC} https://github.com/juzcn/slife"
