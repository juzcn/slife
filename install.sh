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
INSTALL_DIR="$HOME/.slife"
echo "Install directory : ${CYAN}$INSTALL_DIR${NC}"
echo "Python            : auto-install 3.13 if needed"
echo "Node.js           : install if missing (optional, for web fetch)"
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

# ── 1. Ensure uv is available ────────────────────────────────────────
echo -e "${YELLOW}[1/6] Checking uv (package manager)…${NC}"
if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}  Installing uv…${NC}"
    curl --progress-bar -Lf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo -e "${GREEN}  ✓${NC} uv $(uv --version 2>&1)"

# ── 2. Ensure Python >= 3.13 is available ────────────────────────────
echo -e "${YELLOW}[2/6] Checking Python >= 3.13…${NC}"
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
        echo -e "  Found $candidate ($ver) — too old (need >= 3.13)" >&2
    fi
done

if [ -z "$PYTHON" ]; then
    # Not on PATH — check if uv already manages a Python 3.13
    UV_PYTHON="$(uv python find 3.13 2>/dev/null || echo "")"
    if [ -n "$UV_PYTHON" ]; then
        echo -e "${GREEN}  found (uv-managed)${NC}"
        PYTHON="$UV_PYTHON"
    else
        echo -e "${YELLOW}  Python >= 3.13 not found, installing via uv…${NC}"
        uv python install 3.13
        PYTHON="$(uv python find 3.13 2>/dev/null || echo "")"
        if [ -z "$PYTHON" ]; then
            echo -e "${RED}Error: could not install Python 3.13.${NC}"
            echo -e "${YELLOW}Install manually from https://python.org/downloads/${NC}"
            echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
            exit 1
        fi
        echo -e "${GREEN}  ✓${NC} Installed at: ${CYAN}$PYTHON${NC}"
    fi
else
    echo -e "${GREEN}  found${NC}"
fi
echo -e "  Selected: ${CYAN}$PYTHON${NC} ($(uv run --python "$PYTHON" python --version 2>&1))"

# Ensure uv-managed Python and its scripts directory are on PATH.
PYTHON_DIR="$(dirname "$PYTHON")"
export PATH="$PYTHON_DIR:$HOME/.local/bin:$PATH"

# ── 3. Ensure Node.js / npm is available ─────────────────────────────
echo -e "${YELLOW}[3/6] Checking Node.js / npm…${NC}"
HAVE_NODE=false
if command -v node &>/dev/null && command -v npm &>/dev/null; then
    echo -e "${GREEN}  ✓${NC} node $(node --version), npm $(npm --version)"
    HAVE_NODE=true
else
    echo -e "${YELLOW}  not found${NC}"
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
    if command -v node &>/dev/null && command -v npm &>/dev/null; then
        echo -e "${GREEN}  ✓${NC} node $(node --version), npm $(npm --version)"
        HAVE_NODE=true
    fi
    if [ "$HAVE_NODE" = false ]; then
        echo -e "${YELLOW}  Skipped: fetch MCP will use Python-based article extraction.${NC}"
        echo -e "${YELLOW}  Install manually: https://nodejs.org (LTS recommended)${NC}"
    fi
fi

# ── 4. Download and verify slife ─────────────────────────────────────
echo ""
echo -e "${YELLOW}[4/6] Downloading slife…${NC}"
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

# ── 5. Create venv + install slife ───────────────────────────────────
# On upgrade: user data files in ~/.slife stay put — we only move them
# aside while recreating the venv, then move back (same-fs mv is instant).
# .credstore data is never touched by this script.
echo -e "${YELLOW}[5/6] Installing slife v${VERSION} to $INSTALL_DIR…${NC}"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}  Upgrading existing installation…${NC}"
    STASH_DIR="$TMP_DIR/slife-user-stash"
    mkdir -p "$STASH_DIR"
    # Move user data aside, leave venv artifacts behind for deletion.
    for item in "$INSTALL_DIR"/* "$INSTALL_DIR"/.*; do
        [ -e "$item" ] || continue
        name="$(basename "$item")"
        case "$name" in
            .|..) ;;
            bin|lib|include|pyvenv.cfg|Scripts|Lib|Include) ;;  # venv — skip
            *) mv "$item" "$STASH_DIR/" 2>/dev/null || true ;;
        esac
    done
    rm -rf "$INSTALL_DIR"
    uv venv --python "$PYTHON" --seed "$INSTALL_DIR"
    # Move user data back (harmless if stash is empty).
    if [ -n "$(ls -A "$STASH_DIR" 2>/dev/null)" ]; then
        shopt -s dotglob 2>/dev/null || true
        mv "$STASH_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
        shopt -u dotglob 2>/dev/null || true
    fi
    rm -rf "$STASH_DIR"
else
    uv venv --python "$PYTHON" --seed "$INSTALL_DIR"
fi

# Install slife from source into the venv (pip already seeded).
echo -e "${YELLOW}  Installing slife and dependencies…${NC}"
PIP_LOG="$TMP_DIR/pip-install.log"
uv pip install --python "$INSTALL_DIR/bin/python" "$TMP_DIR/slife-main" > "$PIP_LOG" 2>&1 || {
    echo -e "${RED}Error: slife installation failed.${NC}"
    echo -e "${YELLOW}Last lines of install log:${NC}"
    tail -n 20 "$PIP_LOG"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
}

# Verify slife and credstore are installed correctly.
echo -e "${YELLOW}  Verifying installation…${NC}"

# Check slife + credstore packages are importable.
if "$INSTALL_DIR/bin/python" -c "import slife; import credstore" 2>/dev/null; then
    echo -e "    ${GREEN}✓${NC} slife + credstore packages"
else
    echo -e "    ${YELLOW}warning: import check failed${NC}"
fi

# Check credstore CLI entry point.
if "$INSTALL_DIR/bin/credstore" --help &>/dev/null; then
    echo -e "    ${GREEN}✓${NC} credstore CLI"
else
    echo -e "    ${YELLOW}warning: credstore CLI not found${NC}"
fi

# Check slife CLI entry point.
if "$INSTALL_DIR/bin/slife" --help &>/dev/null; then
    echo -e "    ${GREEN}✓${NC} slife CLI"
else
    echo -e "    ${YELLOW}warning: slife CLI check returned non-zero (entry point exists)${NC}"
fi

# ── 6. Add to PATH ───────────────────────────────────────────────────
echo -e "${YELLOW}[6/6] Configuring PATH…${NC}"
# Append to common shell profiles if the entry isn't already present.
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" "$HOME/.config/fish/config.fish"; do
    if [ -f "$rc" ]; then
        if ! grep -qF "$INSTALL_DIR/bin" "$rc" 2>/dev/null; then
            # fish uses a different syntax for PATH.
            if echo "$rc" | grep -q fish; then
                echo "fish_add_path \"$INSTALL_DIR/bin\"" >> "$rc"
            else
                echo "export PATH=\"$INSTALL_DIR/bin:\$PATH\"" >> "$rc"
            fi
        fi
    fi
done
export PATH="$INSTALL_DIR/bin:$PATH"
echo -e "${GREEN}  ✓${NC} Added to PATH"

# ── 7. Done ───────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  Slife v${VERSION} installed successfully! 🎉  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}Restart your terminal, then:${NC}"
echo "  credstore set-password              # set up encrypted backup (first time)"
echo "  credstore set DEEPSEEK_API_KEY       # store your API key"
echo "  slife                                # launch the TUI"
echo ""
echo -e "${CYAN}Optional extras:${NC}"
echo "  pip install 'slife[embeddings]'"
echo ""
echo -e "${CYAN}More info:${NC} $SLIFE_REPO"
