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
    curl --progress-bar -Lf https://astral.sh/uv/install.sh | sh
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
echo -e "  Selected: ${CYAN}$PYTHON${NC} ($(uv run --python "$PYTHON" python --version 2>&1))"

# Ensure uv-managed Python and its scripts directory are on PATH.
# This guarantees "python3" and "python" resolve to the real interpreter,
# not a system stub or missing-command handler.
PYTHON_DIR="$(dirname "$PYTHON")"
export PATH="$PYTHON_DIR:$HOME/.local/bin:$PATH"

# ── 2.5 Ensure Node.js / npm is available ───────────────────────────────
echo -n "Checking for Node.js / npm… "
HAVE_NODE=false
if command -v node &>/dev/null && command -v npm &>/dev/null; then
    echo -e "${GREEN}found${NC}"
    echo -e "  node $(node --version), npm $(npm --version)"
    HAVE_NODE=true
else
    echo -e "${YELLOW}not found${NC}"
    if command -v apt-get &>/dev/null; then
        echo -e "${YELLOW}Installing Node.js via apt…${NC}"
        curl --progress-bar -fL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
        sudo apt-get install -y nodejs
    elif command -v brew &>/dev/null; then
        echo -e "${YELLOW}Installing Node.js via Homebrew…${NC}"
        brew install node
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y nodejs npm
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm nodejs npm
    else
        echo -e "${YELLOW}  Skipped: fetch MCP will use Python-based article extraction.${NC}"
        echo -e "${YELLOW}  Install manually: https://nodejs.org (LTS recommended)${NC}"
    fi
fi

# ── 3. Download and verify slife ────────────────────────────────────
echo ""
echo "Downloading slife…"
curl --progress-bar -fL "$SLIFE_TARBALL" -o "$TMP_DIR/slife.tar.gz"

# Print SHA256 so users can verify integrity if desired.
echo "  SHA256: $(sha256sum "$TMP_DIR/slife.tar.gz" 2>/dev/null || shasum -a 256 "$TMP_DIR/slife.tar.gz" 2>/dev/null || echo '(sha256sum not available)')"

tar xzf "$TMP_DIR/slife.tar.gz" -C "$TMP_DIR"

# Read version from pyproject.toml.
# Use portable sed (no GNU -P extension) so this works on macOS too.
VERSION="unknown"
PYPROJECT="$TMP_DIR/slife-main/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
    EXTRACTED_VERSION=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PYPROJECT" 2>/dev/null || echo "")
    if [ -n "$EXTRACTED_VERSION" ]; then
        VERSION="$EXTRACTED_VERSION"
    fi
fi

INSTALL_DIR="$HOME/.slife"

# ── 4. Disk space check ─────────────────────────────────────────────
# A full install (Python + slife + deps) can use ~500 MB.  Require at
# least 1 GB free to leave breathing room.
if command -v df &>/dev/null; then
    FREE_KB=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
    if [ "$FREE_KB" -gt 0 ] 2>/dev/null && [ "$FREE_KB" -lt 1048576 ]; then
        FREE_MB=$((FREE_KB / 1024))
        echo -e "${YELLOW}Warning: only ~${FREE_MB} MB free on $HOME.${NC}"
        echo "Installation may fail due to insufficient disk space."
    fi
fi

# ── 5. Create venv + install slife ──────────────────────────────────
# On upgrade we keep user data: slife.json5, *.db, logs/, wechat_*.json5,
# credentials.crypt.  uv venv needs the target directory absent, so we
# stash user data → wipe → create venv → restore.
echo "Installing slife v${VERSION} to $INSTALL_DIR…"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}Upgrading existing installation…${NC}"
    BACKUP_DIR="$TMP_DIR/slife-user-backup-$$"
    mkdir -p "$BACKUP_DIR"

    # Save user data — track what we expect to find so we can verify.
    BACKUP_MANIFEST=""
    for item in "$INSTALL_DIR"/*; do
        name="$(basename "$item")"
        case "$name" in
            slife.json5|credentials.crypt|logs)
                cp -R "$item" "$BACKUP_DIR/" && BACKUP_MANIFEST="$BACKUP_MANIFEST $name"
                ;;
            *.db|*.db-shm|*.db-wal)
                cp -R "$item" "$BACKUP_DIR/" && BACKUP_MANIFEST="$BACKUP_MANIFEST $name"
                ;;
            wechat_*.json5)
                cp -R "$item" "$BACKUP_DIR/" && BACKUP_MANIFEST="$BACKUP_MANIFEST $name"
                ;;
        esac
    done

    # Verify critical backups exist before destroying the old install.
    for critical in slife.json5; do
        if [ -f "$INSTALL_DIR/$critical" ] && [ ! -f "$BACKUP_DIR/$critical" ]; then
            echo -e "${RED}Error: failed to back up $critical — aborting upgrade to keep your data safe.${NC}"
            exit 1
        fi
    done

    # Wipe old installation, then create fresh venv.
    # --clear ensures uv handles leftover files even if rm couldn't
    # delete everything (locked files, etc.).
    rm -rf "$INSTALL_DIR"
    uv venv --clear --python "$PYTHON" "$INSTALL_DIR"
    # Restore user data (only if backup has files — avoid glob failure
    # on empty directory with "set -e").
    if [ -d "$BACKUP_DIR" ] && [ "$(ls -A "$BACKUP_DIR" 2>/dev/null)" ]; then
        cp -R "$BACKUP_DIR"/* "$INSTALL_DIR/"
        rm -rf "$BACKUP_DIR"
    fi
else
    uv venv --python "$PYTHON" "$INSTALL_DIR"
fi

# Install pip into the venv (the only seed package we need — slife's
# error messages guide users to run "pip install", so pip must be
# available; setuptools and wheel are unnecessary for end users).
uv pip install --python "$INSTALL_DIR/bin/python" pip

# Install slife from source into the venv.
echo "Installing slife and dependencies…"
PIP_LOG="$TMP_DIR/pip-install.log"
uv pip install --python "$INSTALL_DIR/bin/python" "$TMP_DIR/slife-main" > "$PIP_LOG" 2>&1 || {
    echo -e "${RED}Error: slife installation failed.${NC}"
    echo "Last 40 lines of install log:"
    tail -n 40 "$PIP_LOG"
    exit 1
}

# Verify credstore CLI is available (bundled with slife).
echo -n "Verifying credstore CLI… "
if "$INSTALL_DIR/bin/credstore" --help &>/dev/null; then
    echo -e "${GREEN}✓${NC} ready"
else
    echo -e "${YELLOW}warning: credstore CLI not found${NC}"
fi

# ── 6. Add to PATH ──────────────────────────────────────────────────
# Append to common shell profiles if the entry isn't already present.
# Use grep -F (fixed-string) for robustness against special characters
# in the path.
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

# ── 7. Done ──────────────────────────────────────────────────────────
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
echo -e "${CYAN}More info:${NC} https://github.com/juzcn/slife"
