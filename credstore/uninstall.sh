#!/usr/bin/env bash
set -euo pipefail

# credstore uninstaller for macOS, Linux, and WSL.
# Usage:
#   ./uninstall.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

echo ""
echo -e "${CYAN}credstore Uninstaller${NC}"
echo ""

# 1. Uninstall from uv tool
if uv tool list 2>/dev/null | grep -qF "credstore"; then
    echo -e "${YELLOW}Uninstalling credstore…${NC}"
    if uv tool uninstall credstore 2>&1; then
        echo -e "  ${GREEN}✓${NC} credstore removed"
    else
        echo -e "  ${RED}✗${NC} uninstall failed"
    fi
else
    echo -e "${GRAY}credstore is not installed.${NC}"
fi

# 2. Clean up wrapper binaries
LOCAL_BIN="$HOME/.local/bin"
for bin in "$LOCAL_BIN/credstore"; do
    if [ -f "$bin" ] || [ -L "$bin" ]; then
        rm -f "$bin"
        echo -e "  ${GRAY}Removed: $bin${NC}"
    fi
done

# 3. Remaining data
echo ""
DATA_DIR="$HOME/.credstore"
if [ -d "$DATA_DIR" ]; then
    SIZE=$(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)
    echo -e "${YELLOW}Data files NOT removed (delete manually if desired):${NC}"
    echo -e "${GRAY}  ~/.credstore/           (${SIZE:-?}) — encrypted credential backup${NC}"
else
    echo -e "${GREEN}  ✓${NC} No remaining data files."
fi

echo ""
echo -e "${CYAN}Done.${NC}"
