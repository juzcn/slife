#!/usr/bin/env bash
set -euo pipefail

# cc-switch uninstaller for macOS, Linux, and WSL.
# Usage:
#   ./uninstall.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

echo ""
echo -e "${CYAN}cc-switch Uninstaller${NC}"
echo ""

# 1. Uninstall from uv tool
if uv tool list 2>/dev/null | grep -qF "cc-switch"; then
    echo -e "${YELLOW}Uninstalling cc-switch…${NC}"
    if uv tool uninstall cc-switch 2>&1; then
        echo -e "  ${GREEN}✓${NC} cc-switch removed"
    else
        echo -e "  ${RED}✗${NC} uninstall failed"
    fi
else
    echo -e "${GRAY}cc-switch is not installed.${NC}"
fi

# 2. Clean up wrapper binaries
LOCAL_BIN="$HOME/.local/bin"
for bin in "$LOCAL_BIN/cc-switch"; do
    if [ -f "$bin" ] || [ -L "$bin" ]; then
        rm -f "$bin"
        echo -e "  ${GRAY}Removed: $bin${NC}"
    fi
done

# 3. Remaining data
echo ""
CONFIG_FILE="$HOME/.claude/cc-switch.json"
if [ -f "$CONFIG_FILE" ]; then
    echo -e "${YELLOW}Data files NOT removed (delete manually if desired):${NC}"
    echo -e "${GRAY}  $CONFIG_FILE           — provider/model configs${NC}"
else
    echo -e "${GREEN}  ✓${NC} No remaining data files."
fi

echo ""
echo -e "${CYAN}Done.${NC}"
