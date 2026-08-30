#!/usr/bin/env bash

# ── Piped-stdin guard (WSL interop) ──────────────────────────────
# When bash reads this script from a pipe (curl … | bash), any
# Windows .exe run via WSL interop inherits and may consume the
# pipe and truncate the script.  Save stdin to a temp file first.
# Must run *before* `set -euo pipefail` so CRLF line-endings in a
# Windows-hosted copy don't cause `set … \r` to fail.
if [ ! -t 0 ]; then
    _SLIFE_INSTALL_SCRIPT="$(mktemp)" || exit 1
    cat > "$_SLIFE_INSTALL_SCRIPT"   || exit 1
    sed -i'' -e 's/\r$//' "$_SLIFE_INSTALL_SCRIPT" 2>/dev/null || true
    _SLIFE_CLEANUP_SCRIPT="$_SLIFE_INSTALL_SCRIPT" _SLIFE_PIPED_INSTALL=1 exec bash "$_SLIFE_INSTALL_SCRIPT" "$@"
    exit 1  # exec failed
fi
if [ -n "${_SLIFE_CLEANUP_SCRIPT:-}" ]; then
    trap 'rm -f "$_SLIFE_CLEANUP_SCRIPT"' EXIT
fi

set -euo pipefail

# Slife one-click installer for macOS, Linux, and WSL.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/juzcn/slife/main/install.sh | bash
#
# No prerequisites — the script installs uv if needed, then uses
# ``uv tool install`` to install slife in an isolated environment.
# Python 3.13 is managed automatically by uv.
#
# Full tool set is installed by default (local embeddings, yt-dlp,
# browser-harness).  Pass ``--core`` (or set $SLIFE_CORE=1) for a light
# core install that skips those optional tools.

# --core / $SLIFE_CORE=1 → light install (skip the optional full tool set)
CORE_MODE=false
if [ "${1:-}" = "--core" ]; then CORE_MODE=true; fi
if [ "${SLIFE_CORE:-}" = "1" ]; then CORE_MODE=true; fi

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

echo -e "${CYAN}Slife Installer${NC}"
echo ""

#
echo "Install method    : uv tool install (isolated environment, from source — always latest main)"
echo -e "User data         : ${CYAN}$HOME/.slife/${NC}"
echo "Python            : managed by uv (3.13)"
echo "npx               : auto-install Node.js if needed (required for MCP servers)"
echo "bun               : auto-install bun if needed (required for nvidia-nim MCP)"
echo "unzip             : auto-install on Linux if missing (bun installer dependency)"
echo "rootless fallback : official tarballs → ~/.local when no root/package manager"
echo "Configs           : seeded from bundled defaults (slife / local-embed / mcp-plugin)"
echo "Full tool set     : embeddings, yt-dlp, browser-harness, Mosquitto (--core to skip)"
echo "Disk space needed : ~500 MB (embeddings extra: +0.3–2 GB)"
echo ""

# ── Rootless install helpers ────────────────────────────────────
# On HPC login nodes there is usually no root and no package manager,
# so sudo-based steps fail.  These install official binaries into
# ~/.local — the same rootless pattern uv and bun use.

# Node.js LTS: official binary tarball → ~/.local/lib/nodejs,
# with node/npm/npx symlinked into ~/.local/bin.  Installs the current
# Node LTS (v22.x) from the floating nodejs.org prefix; integrity is
# verified against the checksum file before anything is extracted.
# If the environment can't run it (e.g. old glibc), the install reports
# the missing symbols and the user is pointed at a compatible route.
_slife_install_node_rootless() {
    local _v _sha _file _url _got _rel _arch _root _bin _nixarch _list
    if ! command -v sha256sum &>/dev/null; then
        echo -e "  ${YELLOW}sha256sum not available — skipping tarball install.${NC}"
        return 1
    fi
    _arch="$(uname -m 2>/dev/null)"
    _nixarch=""
    case "$_arch" in
        x86_64|amd64) _nixarch="x64" ;;
        aarch64|arm64) _nixarch="arm64" ;;
    esac
    if [ -z "$_nixarch" ]; then
        echo -e "  ${YELLOW}Unsupported architecture '$_arch' — install Node.js manually.${NC}"
        return 1
    fi
    _v="$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt 2>/dev/null \
            | grep "node-v[0-9.]*-linux-$_nixarch\.tar\.gz" | head -1 || true)"
    [ -n "$_v" ] || { echo -e "  ${YELLOW}Could not resolve latest Node.js LTS — skipping.${NC}"; return 1; }
    _sha="$(echo "$_v" | awk '{print $1}')"
    _file="$(echo "$_v" | awk '{print $2}')"
    _url="https://nodejs.org/dist/latest-v22.x/$_file"
    _root="$HOME/.local/lib/nodejs"
    _bin="$HOME/.local/bin"
    echo -e "  ${GRAY}Downloading $_file…${NC}"
    curl --progress-bar -fL "$_url" -o "$TMP_DIR/node.tar.gz" || return 1
    _got="$(sha256sum "$TMP_DIR/node.tar.gz" 2>/dev/null | awk '{print $1}')"
    if [ -z "$_got" ] || [ "$_got" != "$_sha" ]; then
        echo -e "  ${YELLOW}SHA256 mismatch — skipping tarball install.${NC}"
        return 1
    fi
    mkdir -p "$_root" "$_bin"
    # Clean any stale partial install of the same dir name.
    rm -rf "$_root/node-v"* 2>/dev/null || true
    if ! tar -xzf "$TMP_DIR/node.tar.gz" -C "$_root" --strip-components=1; then
        echo -e "  ${YELLOW}Extraction failed — skipping tarball install.${NC}"
        return 1
    fi
    # Drop symlinks before re-linking so an older install can't linger.
    _list="node npm npx"
    for _rel in $_list; do
        [ -e "$_bin/$_rel" ] && rm -f "$_bin/$_rel" 2>/dev/null || true
    done
    for _rel in $_list; do
        ln -sf "$_root/bin/$_rel" "$_bin/$_rel" 2>/dev/null || true
    done
    export PATH="$_bin:$PATH"
    [ -x "$_bin/npx" ] && [ -f "$_root/lib/node_modules/npm/bin/npm-cli.js" ]
}

# Python ≥3.8 available?  (bun's flat zip needs no `unzip`, only zipfile.)
_slife_have_python() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null
}

# bun: official installer normally (no root needed — installs to ~/.bun).
# If `unzip` is missing (no package manager to install it), extract the
# same official bun-<platform>.zip with Python's zipfile instead.
_slife_install_bun() {
    if command -v unzip &>/dev/null; then
        curl -fsSL https://bun.sh/install | bash
        return $?
    fi
    if ! _slife_have_python; then
        echo -e "  ${YELLOW}unzip missing and no Python ≥3.8 — falling back to official installer.${NC}"
        curl -fsSL https://bun.sh/install | bash
        return $?
    fi
    local _arch _nixarch _url _bin _exe _dir _ver
    _arch="$(uname -m 2>/dev/null)"
    _nixarch=""
    case "$_arch" in
        x86_64|amd64) _nixarch="linux-x64" ;;
        aarch64|arm64) _nixarch="linux-aarch64" ;;
    esac
    if [ -z "$_nixarch" ]; then
        echo -e "  ${YELLOW}Unsupported architecture '$_arch' — using official installer.${NC}"
        curl -fsSL https://bun.sh/install | bash
        return $?
    fi
    # Download from npmmirror (reachable in mainland China) first, then
    # GitHub.  npmmirror has no `latest` alias, so resolve the newest
    # version from its directory listing.
    _url=""
    if curl -fsSL --max-time 15 https://registry.npmmirror.com/-/binary/bun/ 2>/dev/null \
            | grep -oE 'bun-v[0-9]+\.[0-9]+\.[0-9]+/' \
            | grep -oE 'bun-v[0-9.]+' | sort -V | tail -1 | grep -q .; then
        _ver="$(curl -fsSL --max-time 15 https://registry.npmmirror.com/-/binary/bun/ 2>/dev/null \
                | grep -oE 'bun-v[0-9]+\.[0-9]+\.[0-9]+/' \
                | grep -oE 'bun-v[0-9.]+' | sort -V | tail -1)"
        _url="https://registry.npmmirror.com/-/binary/bun/$_ver/$_nixarch.zip"
    fi
    if [ -z "$_url" ]; then
        _url="https://github.com/oven-sh/bun/releases/latest/download/bun-$_nixarch.zip"
    fi
    echo -e "  ${YELLOW}No unzip — extracting bun with Python zipfile…${NC}"
    curl --progress-bar -fL "$_url" -o "$TMP_DIR/bun.zip" || return 1
    _bin="$HOME/.bun/bin"
    mkdir -p "$_bin"
    if ! python3 - "$TMP_DIR/bun.zip" "$_bin" <<'PYEOF'
import sys, zipfile
zp, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zp) as z:
    names = z.namelist()
    tops = {n.split('/', 1)[0] for n in names}
    # single top dir (bun-linux-x64/) → flatten onto out
    if len(tops) == 1:
        for n in names:
            z.extract(n, out)
    else:
        z.extractall(out)
PYEOF
    then
        echo -e "  ${YELLOW}bun extraction failed — skipping.${NC}"
        return 1
    fi
    # flatten single top dir if present (bun-linux-x64/bun → out/bun)
    _exe="$HOME/.bun/bin/bun"
    if [ ! -x "$_exe" ]; then
        _dir="$(find "$HOME/.bun/bin" -maxdepth 1 -type d -name 'bun-*' 2>/dev/null | head -1 || true)"
        [ -n "$_dir" ] && mv "$_dir"/* "$HOME/.bun/bin/" 2>/dev/null || true
    fi
    chmod +x "$_exe" 2>/dev/null || true
    [ -x "$_exe" ]
}

#
if command -v df &>/dev/null; then
    FREE_KB=$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
    if [ "$FREE_KB" -gt 0 ] 2>/dev/null && [ "$FREE_KB" -lt 1048576 ]; then
        FREE_MB=$((FREE_KB / 1024))
        echo -e "${RED}Error: only ~${FREE_MB} MB free on $HOME (need >= 1 GB).${NC}"
        echo -e "${YELLOW}Free up space and try again.  Help: $SLIFE_REPO${NC}"
        exit 1
    fi
fi

#
echo -e "${YELLOW}[1/5] Ensuring uv is available…${NC}"
if ! command -v uv &>/dev/null; then
    echo -e "${YELLOW}  Installing uv…${NC}"
    curl --progress-bar -Lf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo -e "${GREEN}  ✓${NC} uv $(uv --version 2>&1)"

#
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
    # On HPC login nodes there is usually no root, so sudo steps fail
    # (harmlessly, via `|| true`).  Try a preloaded cluster module first —
    # modules are read-only, no root needed.
    if command -v module &>/dev/null && module load nodejs 2>/dev/null && command -v npx &>/dev/null; then
        echo -e "${GREEN}  ✓${NC} npx v$(npx --version 2>&1) (via module load nodejs)"
        HAVE_NPX=true
    fi
    # Rootless fallback: official Node.js binary tarball → ~/.local
    # (exactly how uv and bun are installed — no root, no package manager).
    if [ "$HAVE_NPX" = false ] && command -v tar &>/dev/null; then
        echo -e "${YELLOW}  Installing Node.js LTS via official tarball → ~/.local…${NC}"
        # Subshell + set +e: a failure here is only a warning — it must
        # never abort the whole install (runtime tools are optional).
        if ( set +e; _slife_install_node_rootless; ); then
            HAVE_NPX=true
        else
            echo -e "${YELLOW}  Node.js tarball install failed — install it manually and re-run.${NC}"
        fi
    fi
    if [ "$HAVE_NPX" = true ]; then
        echo -e "${GREEN}  ✓${NC} npx v$(npx --version 2>&1)"
    else
        echo -e "${RED}WARNING: npx not available.${NC}"
        echo -e "${RED}  These MCP servers require npx and will NOT work:${NC}"
        echo -e "${RED}    file-search, serper, tavily-mcp, github, amap-maps, filesystem${NC}"
        echo -e "${RED}  Install Node.js LTS from https://nodejs.org then re-run this installer.${NC}"
        echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    fi
fi

#
echo -e "${YELLOW}[2b] Ensuring bun (JavaScript runtime) is available…${NC}"
HAVE_BUN=false
if command -v bun &>/dev/null; then
    echo -e "${GREEN}  ✓${NC} bun v$(bun --version 2>&1)"
    HAVE_BUN=true
elif [ -x "$HOME/.bun/bin/bun" ]; then
    echo -e "${GREEN}  ✓${NC} bun v$($HOME/.bun/bin/bun --version 2>&1) (found at ~/.bun/bin/bun)"
    export PATH="$HOME/.bun/bin:$PATH"
    HAVE_BUN=true
fi

if [ "$HAVE_BUN" = false ]; then
    echo -e "${YELLOW}  bun not found, installing…${NC}"
    # bun's installer requires unzip
    if ! command -v unzip &>/dev/null; then
        echo -e "${YELLOW}  Installing unzip (bun installer dependency)…${NC}"
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y unzip 2>/dev/null || true
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y unzip 2>/dev/null || true
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm unzip 2>/dev/null || true
        fi
        # Rootless fallback (no root / no package manager): Python's
        # zipfile extracts the flat bun zip just as well.  No Python
        # here is not an error — the official bun installer is still
        # attempted below and will report its own failure if needed.
        if ! command -v unzip &>/dev/null && ! _slife_have_python; then
            echo -e "${YELLOW}  unzip unavailable and no Python ≥3.8 — will warn instead of fail.${NC}"
        fi
    fi
    # Subshell + set +e: a bun failure is only a warning — it must never
    # abort the whole install (runtime tools are optional).
    if ( set +e; _slife_install_bun; ); then
        echo -e "  ${GRAY}✓${NC} bun installed via official installer"
    else
        echo -e "  ${YELLOW}⚠ bun install failed — see messages above.${NC}"
    fi
    export PATH="$HOME/.bun/bin:$PATH"
    # Persist bun in shell profile so it survives reboot.
    for _rc in "$HOME/.bashrc" "$HOME/.profile"; do
        if [ -f "$_rc" ] && ! grep -qF '$HOME/.bun/bin' "$_rc" 2>/dev/null; then
            echo 'export PATH="$HOME/.bun/bin:$PATH"' >> "$_rc"
        fi
    done
    if command -v bun &>/dev/null; then
        echo -e "${GREEN}  ✓${NC} bun v$(bun --version 2>&1)"
        HAVE_BUN=true
    fi
    if [ "$HAVE_BUN" = false ]; then
        echo -e "${RED}WARNING: bun not available.${NC}"
        echo -e "${RED}  The nvidia-nim MCP server requires bunx and will NOT work.${NC}"
        echo -e "${RED}  Install bun manually from https://bun.sh then re-run this installer.${NC}"
        echo -e "${RED}  All other MCP servers (npx-based) are unaffected.${NC}"
    fi
fi

#
echo -e "${YELLOW}[optional] Ensuring Mosquitto (MQTT broker for multi-agent mesh)…${NC}"
if command -v mosquitto &>/dev/null; then
    echo -e "${GREEN}  ✓${NC} mosquitto found"
else
    echo -e "${GRAY}  Mosquitto not found — installing automatically (A2A mesh)…${NC}"
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y mosquitto mosquitto-clients 2>/dev/null || true
    elif command -v brew &>/dev/null; then
        brew install mosquitto 2>/dev/null || true
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y mosquitto 2>/dev/null || true
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm mosquitto 2>/dev/null || true
    fi
    # Re-check
    if command -v mosquitto &>/dev/null; then
        echo -e "${GREEN}  ✓${NC} Mosquitto installed"
        echo -e "${CYAN}  To start Mosquitto:${NC}"
        echo "    mosquitto -d"
        echo -e "${CYAN}  Or as a system service:${NC}"
        echo "    sudo systemctl enable --now mosquitto   # systemd (Linux)"
        echo "    brew services start mosquitto           # Homebrew (macOS)"
    else
        echo -e "${YELLOW}  Mosquitto unavailable — A2A mesh disabled until it runs.${NC}"
        echo -e "${YELLOW}  Install manually: https://mosquitto.org/download/${NC}"
    fi
fi

#
echo ""
echo -e "${YELLOW}[3/5] Downloading slife…${NC}"
# Try GitHub first, then the gitee mirror (GitHub is often unreachable
# from mainland China / HPC login nodes).  A failure here is a hard
# error — slife itself cannot be installed without its source — but it
# is reported cleanly instead of tripping set -e.
SLIFE_DL_OK=false
if curl --progress-bar -fL "$SLIFE_TARBALL" -o "$TMP_DIR/slife.tar.gz"; then
    SLIFE_DL_OK=true
else
    echo -e "  ${YELLOW}GitHub unreachable — trying gitee mirror…${NC}"
    # gitee uses /repository/archive/{branch}.tar.gz, NOT GitHub's
    # /archive/refs/heads/{branch}.tar.gz format (that 404s on gitee).
    if curl --progress-bar -fL "https://gitee.com/juzcn/slife/repository/archive/main.tar.gz" -o "$TMP_DIR/slife.tar.gz"; then
        SLIFE_DL_OK=true
    fi
fi
if [ "$SLIFE_DL_OK" = false ]; then
    echo -e "${RED}Error: could not download slife from GitHub or gitee.${NC}"
    echo -e "${YELLOW}  Check your network, or download manually and re-run.${NC}"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
fi

# Print SHA256 so users can verify integrity if desired.
echo -e "  SHA256: ${GRAY}$(sha256sum "$TMP_DIR/slife.tar.gz" 2>/dev/null || shasum -a 256 "$TMP_DIR/slife.tar.gz" 2>/dev/null || echo '(sha256sum not available)')${NC}"

tar xzf "$TMP_DIR/slife.tar.gz" -C "$TMP_DIR"

# Read version + extra-index-url from pyproject.toml.
VERSION="unknown"
EXTRA_INDEX_ARGS=""
PYPROJECT="$TMP_DIR/slife-main/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
    EXTRACTED_VERSION=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PYPROJECT" 2>/dev/null || echo "")
    if [ -n "$EXTRACTED_VERSION" ]; then
        VERSION="$EXTRACTED_VERSION"
    fi
    _url=$(grep -A2 'extra-index-url' "$PYPROJECT" 2>/dev/null | grep -o 'https\?://[^"]*' | head -1 || true)
    [ -n "$_url" ] && EXTRA_INDEX_ARGS="--extra-index-url $_url"
fi

#
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
                echo -e "  ${GRAY}Captured $_count packages — will diff against fresh install to find user-added ones${NC}"
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

# Mainland-China / HPC nodes: GitHub is unreachable, but `uv tool install`
# still needs to fetch the Python 3.13 standalone build from
# github.com/astral-sh/python-build-standalone — and it hangs with no
# visible progress.  Same fail-open pattern as the slife tarball / bun:
# probe GitHub once; if it's blocked, point uv at a github-release mirror
# (NJU mirrors astral-sh/python-build-standalone; Tsinghua does not).
# Never override a mirror the user already set.
if [ -z "${UV_PYTHON_INSTALL_MIRROR:-}" ] && \
   ! curl -fsSL --max-time 8 -o /dev/null https://github.com 2>/dev/null; then
    export UV_PYTHON_INSTALL_MIRROR="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
    echo -e "  ${GRAY}GitHub unreachable — Python 3.13 downloads via NJU mirror${NC}"
fi

TOOL_INSTALL_LOG="$TMP_DIR/tool-install.log"
set +eo pipefail
uv tool install --from "$TMP_DIR/slife-main" --python 3.13 slife > "$TOOL_INSTALL_LOG" 2>&1
INSTALL_EXIT=$?
set -eo pipefail
if [ $INSTALL_EXIT -ne 0 ]; then
    echo -e "${RED}Error: slife installation failed.${NC}"
    echo -e "${YELLOW}Last lines of install log:${NC}"
    tail -n 20 "$TOOL_INSTALL_LOG"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
fi

# Re-add preserved packages into the new tool venv.
if [ -s "$PRESERVED_REQS" ]; then
    NEW_LINE=$(uv tool list --show-paths 2>/dev/null | grep "slife v" || true)
    NEW_VENV=$(echo "$NEW_LINE" | sed -n 's/.*(\(.*\)).*/\1/p')
    if [ -n "$NEW_VENV" ] && [ -d "$NEW_VENV" ]; then
        NEW_PYTHON="$NEW_VENV/bin/python"

        # Diff old freeze against new venv — only re-add packages not
        # already in the base install (avoids conflicts with transitive deps).
        EXTRA_REQS="${TMPDIR:-/tmp}/slife-extra-requirements.txt"
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


#
# ── Lightweight extras (out-of-the-box; skip with --core) ────────────────
# The heavy semantic packages (sentence-transformers, llama-cpp-python) are
# deliberately NOT installed here — see step [4d]: they are env-specific
# (CPU / CUDA / Metal builds) and paired with a long model download, so the
# user runs those commands themselves after the install.  This step only
# adds the small CLI tools.
echo -e "${YELLOW}[4b] Installing lightweight CLI tools (yt-dlp, browser-harness)…${NC}"
if [ "$CORE_MODE" = true ]; then
    echo -e "${GRAY}  --core: skipping CLI tools${NC}"
else
    TOOL_PY="$(uv tool dir)/slife/bin/python" 2>/dev/null || true
    if [ -x "$TOOL_PY" ]; then
        # yt-dlp — plain PyPI, no extra index needed.
        if uv pip install --python "$TOOL_PY" yt-dlp >> "$TOOL_INSTALL_LOG" 2>&1; then
            echo -e "${GREEN}  ✓${NC} yt-dlp"
        else
            echo -e "${YELLOW}  ⚠ yt-dlp install failed (optional) — log tail:${NC}"
            tail -8 "$TOOL_INSTALL_LOG" | while IFS= read -r _l; do echo -e "    ${GRAY}$_l${NC}"; done
        fi
        # browser-harness (declared in the default config's cli_tools)
        if uv tool install --python 3.12 --upgrade --force browser-harness \
            >> "$TOOL_INSTALL_LOG" 2>&1; then
            echo -e "${GREEN}  ✓${NC} browser-harness (uv tool)"
        else
            echo -e "${YELLOW}  ⚠ browser-harness install failed (optional)${NC}"
        fi
    else
        echo -e "${YELLOW}  ⚠ tool venv not found — skipped CLI tools (optional)${NC}"
    fi
fi

# ── Configs: seed the git-tracked defaults out-of-the-box ───────────────
# slife.json5 / local_embed.json5 / mcp-plugin.json5 come from the downloaded
# source tree (now git-tracked).  Each module hosts its own config in its own
# data dir (~/.slife, ~/.local-embed, ~/.mcp-plugin).  Missing ones are copied
# silently; an existing one is only replaced after a per-file "yes".
echo -e "${YELLOW}[4c] Setting up configs (out-of-the-box defaults)…${NC}"
SEED_DIR="$TMP_DIR/slife-main"
for _name in slife.json5 local_embed.json5 mcp-plugin.json5; do
    _src="$SEED_DIR/$_name"
    [ -f "$_src" ] || continue   # older main snapshots may lack the seeds
    # Each module's config lives in its own folder.
    case "$_name" in
        local_embed.json5) _target="$HOME/.local-embed/local_embed.json5" ;;
        mcp-plugin.json5)  _target="$HOME/.mcp-plugin/mcp-plugin.json5" ;;
        *)                 _target="$HOME/.slife/slife.json5" ;;
    esac
    mkdir -p "$(dirname "$_target")" 2>/dev/null || true
    if [ -e "$_target" ]; then
        # Ask per file — the user may have customized one config but want
        # defaults for another; never force a reset on all of them together.
        _ask="n"
        if [ -t 0 ]; then
            read -p "  Reset $_target to the bundled default? (y/N, default: N): " _ask
        fi
        if [ "$_ask" = "y" ] || [ "$_ask" = "Y" ]; then
            if cp -f "$_src" "$_target" 2>/dev/null && chmod 600 "$_target" 2>/dev/null; then
                echo -e "  ${GRAY}reset  $_target${NC}"
            else
                echo -e "  ${RED}⚠ could not write $_target${NC}"
            fi
        fi
    else
        if cp "$_src" "$_target" 2>/dev/null && chmod 600 "$_target" 2>/dev/null; then
            echo -e "  ${GRAY}seeded $_target${NC}"
        else
            echo -e "  ${RED}⚠ could not write $_target${NC}"
        fi
    fi
done

#
# ── Semantic memory search — user-run setup ──────────────────────────────
# Semantic / hybrid memory search needs a model AND one embedding backend.
# The installer does NOT install these by default: the backend build is
# env-specific (CPU / CUDA / Metal) and the model download is ~2 GB, so the
# user picks the matching command and runs it in a terminal afterwards.
# Everything below is the same feature — set the backend first, then the model.
echo -e "${YELLOW}[4d] Semantic memory search — setup commands (run in a terminal)…${NC}"
SEMANTIC_PY="$(uv tool dir)/slife/bin/python"
SEMANTIC_BIN="$(uv tool dir)/slife/bin/local-embed"
echo -e "${GRAY}  Semantic memory search needs a model AND an embedding backend.  Choose ONE backend:${NC}"
echo -e "${CYAN}    # sentence-transformers — simplest, works everywhere:${NC}"
echo -e "    uv pip install --python \"$SEMANTIC_PY\" sentence-transformers"
echo -e "${CYAN}    # llama-cpp-python — NVIDIA GPU (CUDA 12):${NC}"
echo -e "    uv pip install --python \"$SEMANTIC_PY\" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 llama-cpp-python==0.3.34"
echo -e "${CYAN}    # llama-cpp-python — CPU:${NC}"
echo -e "    uv pip install --python \"$SEMANTIC_PY\" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu llama-cpp-python==0.3.34"
echo -e "${CYAN}    # llama-cpp-python — macOS (Metal):${NC}"
echo -e "    uv pip install --python \"$SEMANTIC_PY\" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/metal llama-cpp-python==0.3.34"
echo -e "${GRAY}  Then download the model:${NC}"
echo -e "${CYAN}    # transformer (~2 GB) — huggingface.co, falls back to hf-mirror.com automatically:${NC}"
echo -e "    \"$SEMANTIC_BIN\" download BAAI/bge-m3"
echo -e "${CYAN}    # or a small GGUF (~100 MB) placed at ~/.slife/models/bge-m3-q4_k_m.gguf${NC}"
echo -e "${GRAY}    (or set the BGE_M3_GGUF_PATH env var) — details in ~/.local-embed/local_embed.json5.${NC}"

#
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
    if [ -n "${_SLIFE_PIPED_INSTALL:-}" ]; then
        echo -e "  ${YELLOW}  NOTE: piped install — this PATH is set only in the installer subshell.${NC}"
        echo -e "  ${YELLOW}  Your interactive shell won't find slife until you reload.${NC}"
    fi
else
    echo -e "  ${RED}  ⚠ slife binary not found on PATH${NC}"
fi

# Done

echo ""
echo -e "${GREEN}Slife v${VERSION} installed successfully!${NC}"
echo ""

# When piped to bash, the exports above only affect the script's subshell —
# not the user's interactive shell.  Remind them to refresh their PATH.
NEEDS_SHELL_REFRESH=true
# When piped to bash, the script runs in a subshell where ~/.local/bin
# was added to PATH by step 1 — so command -v succeeds but the user's
# interactive shell still doesn't have it.  Force the reminder.
if [ -z "${_SLIFE_PIPED_INSTALL:-}" ] && command -v slife &>/dev/null; then
    NEEDS_SHELL_REFRESH=false
fi

if [ "$NEEDS_SHELL_REFRESH" = true ]; then
    echo -e "${YELLOW}IMPORTANT: slife is installed but not on your current PATH.${NC}"
    echo -e "${YELLOW}  Run: source "$HOME/.local/bin/env"${NC}"
    echo -e "${YELLOW}  Or simply open a new terminal.${NC}"
    echo ""
fi

echo -e "${CYAN}Get started:${NC}"
echo "  1. credstore set-password                # encrypted backup (first time)"
echo "  2. credstore set DEEPSEEK_API_KEY        # your first API key (or the one your active model needs — see ~/.slife/slife.json5)"
echo "  3. slife                                 # launch the TUI"
echo ""
if [ -n "${EXTRA_REQS:-}" ] && [ -s "$EXTRA_REQS" ]; then
    if [ "${PRESERVE_OK:-0}" = "1" ]; then
        _count=$(wc -l < "$EXTRA_REQS" 2>/dev/null || echo 0)
        echo -e "${CYAN}Preserved packages:${NC}"
        echo -e "  ${GREEN}✓${NC} $_count extra packages restored from previous install"
    else
        echo -e "${YELLOW}Failed to restore user-added packages — run manually:${NC}"
        echo -e "${YELLOW}  uv pip install -r $EXTRA_REQS${NC}"
    fi
fi
if [ "$CORE_MODE" = true ]; then
    echo -e "${CYAN}Core install done${NC}:"
    echo "  • external MCP servers, Mosquitto (A2A mesh)"
    echo "  • yt-dlp / browser-harness skipped — add later: uv tool install --python 3.12 browser-harness"
else
    echo -e "${CYAN}Installed:${NC}"
    echo "  • external MCP servers, yt-dlp, browser-harness, Mosquitto (A2A mesh)"
fi
echo "  • semantic memory search: run the [4d] commands above (keyword search already works)"
echo "  • mcp-plugin build: \"\$(uv tool dir)/slife/bin/mcp-plugin\" build   # (re)build the MCP server catalog"
echo ""
echo -e "${CYAN}More info:${NC} $SLIFE_REPO"
