#!/usr/bin/env bash
set -euo pipefail

# Slife uninstaller for macOS, Linux, and WSL.
# Usage:
#   ./uninstall.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m'

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║             Slife Uninstaller              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Uninstall from uv tool ──────────────────────────────────────────
if uv tool list 2>/dev/null | grep -qF "slife"; then
    echo -e "${YELLOW}Uninstalling slife (slife + credstore share the same venv)…${NC}"
    if uv tool uninstall slife 2>&1; then
        echo -e "  ${GREEN}✓${NC} slife + credstore removed"
    else
        echo -e "  ${RED}✗${NC} uninstall failed"
    fi
else
    echo -e "${GRAY}slife is not installed.${NC}"
fi

# ── 2. Clean up wrapper binaries ──────────────────────────────────────
LOCAL_BIN="$HOME/.local/bin"
for bin in "$LOCAL_BIN/slife" "$LOCAL_BIN/credstore"; do
    if [ -f "$bin" ] || [ -L "$bin" ]; then
        rm -f "$bin"
        echo -e "  ${GRAY}Removed: $bin${NC}"
    fi
done

# ── 3. Remaining data ─────────────────────────────────────────────────
echo ""
DATA_DIR="$HOME/.slife"

REMAIN=()
if [ -d "$DATA_DIR" ]; then
    SIZE=$(du -sh "$DATA_DIR" 2>/dev/null | cut -f1)
    REMAIN+=("  ~/.slife/           (${SIZE:-?}) — config, logs, databases, skills")
fi
if [ -d "$HOME/.credstore" ]; then
    REMAIN+=("  ~/.credstore/       — encrypted credential backup")
fi

if [ ${#REMAIN[@]} -gt 0 ]; then
    echo -e "${YELLOW}Data files NOT removed (delete manually if desired):${NC}"
    for r in "${REMAIN[@]}"; do
        echo -e "${GRAY}$r${NC}"
    done
else
    echo -e "${GREEN}  ✓${NC} No remaining data files."
fi

echo ""
echo -e "${CYAN}Done.${NC}"
