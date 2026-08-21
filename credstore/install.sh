#!/usr/bin/env bash
set -euo pipefail

# credstore one-click installer for macOS, Linux, and WSL.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/credstore/install.sh | bash
#
# No prerequisites — the script installs uv if needed, then uses
# ``uv tool install`` to install credstore in an isolated environment.
# Python is managed automatically by uv.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

REPO="https://github.com/juzcn/slife"

echo -e "${CYAN}credstore Installer${NC}"
echo ""
echo "Install method : uv tool install (isolated environment)"
echo "User data      : ~/.credstore/ (encrypted credential backup)"
echo "Python         : managed by uv"
echo ""

# [1/2] Ensure uv is available.
if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}[1/2] Installing uv…${NC}"
    curl --progress-bar -Lf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo -e "${GREEN}  ✓${NC} uv $(uv --version 2>&1)"

# [2/2] Install credstore from PyPI.
echo -e "${YELLOW}[2/2] Installing credstore…${NC}"
if uv tool install credstore; then
    echo -e "${GREEN}  ✓${NC} credstore ready"
else
    echo -e "${RED}Error: credstore installation failed.${NC}"
    echo -e "${YELLOW}Help: $REPO${NC}"
    exit 1
fi

export PATH="$HOME/.local/bin:$PATH"

# When piped to bash, the export above only affects this subshell.
if ! command -v credstore &>/dev/null; then
    echo ""
    echo -e "${YELLOW}IMPORTANT: credstore is installed but not on your current PATH.${NC}"
    echo -e "${YELLOW}  Run: source "$HOME/.local/bin/env"${NC}"
    echo -e "${YELLOW}  Or simply open a new terminal.${NC}"
fi

echo ""
echo -e "${GREEN}credstore installed successfully!${NC}"
echo ""
echo -e "${CYAN}Get started:${NC}"
echo "  credstore set-password    # set up encrypted backup (first time)"
echo "  credstore set API_KEY     # store a secret (masked input)"
echo ""
echo -e "${CYAN}More info:${NC} $REPO"
