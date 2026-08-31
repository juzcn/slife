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
# Lightweight tool set is installed by default (yt-dlp, browser-harness).
# The heavy semantic packages (sentence-transformers / llama-cpp-python) are
# NOT — they are a user-run setup printed at the end (step [4d]).
# Pass ``--core`` (or set $SLIFE_CORE=1) to skip even the lightweight tools.
# Mosquitto (A2A mesh broker) installs rootless by default; set $SLIFE_SKIP_MOSQUITTO=1
# to skip it entirely.

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
echo "Full tool set     : yt-dlp, browser-harness, Mosquitto (rootless — no sudo; --core to skip)"
echo "Disk space needed : ~500 MB (semantic setup adds 0.3–2 GB, user-run)"
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

# Mosquitto (A2A mesh broker) — rootless install is the DEFAULT.
# ``apt-get download`` + ``dpkg -x`` fetch and unpack the .deb without root,
# the same rootless pattern used above for node/bun.  If no rootless route
# works we WARN and leave the mesh disabled — never auto-sudo, because a
# piped / non-TTY install would hang on the password prompt.
_slife_install_mosquitto_rootless() {
    local _bin="$HOME/.local/bin" _deb_dir="$TMP_DIR/mosquitto-debs"
    local _pkg _page _url _codename _arch _deb _conf
    mkdir -p "$_bin" "$_deb_dir" || return 1

    # 1. Get the .deb(s) without root (needs apt package lists).
    ( cd "$_deb_dir" && timeout 90 apt-get download mosquitto mosquitto-clients ) >/dev/null 2>&1 || true

    # 1b. Lists missing?  Resolve straight from packages.ubuntu.com (no apt state, no root).
    if ! ls "$_deb_dir"/*.deb >/dev/null 2>&1; then
        _codename="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null | head -1)"
        _arch="$(dpkg --print-architecture 2>/dev/null || true)"
        if [ -n "$_codename" ] && [ -n "$_arch" ]; then
            for _pkg in mosquitto mosquitto-clients; do
                _page="$(curl -fsSL --max-time 20 "https://packages.ubuntu.com/$_codename/$_arch/$_pkg/download" 2>/dev/null || true)"
                _url="$(printf '%s' "$_page" | grep -oE "https?://[^\"' ]*${_pkg}[^\"' ]*\\.deb" | head -1 || true)"
                [ -n "$_url" ] && ( cd "$_deb_dir" && curl -fsSL --max-time 60 -o "$_pkg.deb" "$_url" ) >/dev/null 2>&1 || true
            done
        fi
    fi
    ls "$_deb_dir"/*.deb >/dev/null 2>&1 || return 1

    # 2. Extract into ~/.local (dpkg -x needs no root).
    for _deb in "$_deb_dir"/*.deb; do
        dpkg -x "$_deb" "$HOME/.local" 2>/dev/null || return 1
    done

    # 3. Link binaries onto PATH.
    ln -sf "$HOME/.local/usr/sbin/mosquitto" "$_bin/mosquitto" 2>/dev/null || true
    for _c in mosquitto_passwd mosquitto_pub mosquitto_sub; do
        [ -e "$HOME/.local/usr/bin/$_c" ] && ln -sf "$HOME/.local/usr/bin/$_c" "$_bin/$_c" 2>/dev/null || true
    done
    [ -x "$_bin/mosquitto" ] || return 1

    # 4. Seed a minimal local config — the stock one points at /etc and
    #    /var/lib, which a user prefix doesn't own.  1883 is unprivileged.
    _conf="$HOME/.local/etc/mosquitto/mosquitto.conf"
    if mkdir -p "$(dirname "$_conf")" 2>/dev/null; then
        printf 'listener 1883 127.0.0.1\nallow_anonymous true\npersistence false\n' > "$_conf" 2>/dev/null || true
    fi
    echo -e "  ${GRAY}config: $_conf${NC}"
    echo -e "  ${GRAY}start:  $_bin/mosquitto -c $_conf -d${NC}"
    return 0
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
HAVE_MOSQUITTO=false
if command -v mosquitto >/dev/null 2>&1 || [ -x "$HOME/.local/bin/mosquitto" ]; then
    echo -e "${GREEN}  ✓${NC} mosquitto found"
    HAVE_MOSQUITTO=true
elif [ "$CORE_MODE" = true ]; then
    echo -e "${GRAY}  --core: skipping Mosquitto${NC}"
    HAVE_MOSQUITTO=true
elif [ "${SLIFE_SKIP_MOSQUITTO:-0}" = "1" ]; then
    echo -e "${GRAY}  SLIFE_SKIP_MOSQUITTO=1: skipping Mosquitto${NC}"
    HAVE_MOSQUITTO=true
fi

if [ "$HAVE_MOSQUITTO" = false ]; then
    echo -e "${GRAY}  Mosquitto not found — installing rootless (no sudo)…${NC}"
    if command -v brew &>/dev/null; then
        # Homebrew is user-prefix already — no root involved.
        if brew install mosquitto 2>/dev/null; then
            echo -e "${GREEN}  ✓${NC} Mosquitto installed (Homebrew)"
            echo -e "${CYAN}  Start it for the A2A mesh:${NC}"
            echo "    brew services start mosquitto"
        else
            echo -e "${YELLOW}  Mosquitto unavailable — A2A mesh disabled until it runs.${NC}"
            echo -e "${YELLOW}  Docs: https://mosquitto.org/download/${NC}"
        fi
    elif ( set +e; _slife_install_mosquitto_rootless; ); then
        echo -e "${GREEN}  ✓${NC} Mosquitto installed (rootless → ~/.local)"
        echo -e "${CYAN}  Start it for the A2A mesh:${NC}"
        echo "    ~/.local/bin/mosquitto -c ~/.local/etc/mosquitto/mosquitto.conf -d"
    else
        echo -e "${YELLOW}  Mosquitto unavailable — A2A mesh disabled until it runs.${NC}"
        echo -e "${YELLOW}  Install manually (rootless):${NC}"
        echo '    cd "$(mktemp -d)" && apt-get download mosquitto mosquitto-clients && dpkg -x ./*.deb "$HOME/.local"'
        echo '    "$HOME/.local/bin/mosquitto" -c "$HOME/.local/etc/mosquitto/mosquitto.conf" -d'
        echo -e "${YELLOW}  Or with sudo (system service):${NC}"
        echo "    sudo apt-get install -y mosquitto mosquitto-clients"
        echo -e "${YELLOW}  Docs: https://mosquitto.org/download/${NC}"
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

# Build local wheels for the whole workspace (slife + credstore + cc-switch +
# mcp-plugin + local-embed) and install slife from them.
#
# Why wheels and not `--from "$TMP_DIR/slife-main"`: `uv tool install --from`
# materialises the workspace members (mcp-plugin / local-embed / credstore) as
# EDITABLE installs pointing at the extracted source dir — which the installer
# deletes at the end — so `import mcp_plugin` breaks after a fresh install
# (the members are declared deps; they belong inside slife's venv, as real
# copies).  Local wheels install them as non-editable copies: self-contained,
# survives the temp-dir cleanup, still 100 % from source, no PyPI.
WHEELHOUSE="$TMP_DIR/wheelhouse"
mkdir -p "$WHEELHOUSE"
set +eo pipefail
uv build --all-packages --out-dir "$WHEELHOUSE" "$TMP_DIR/slife-main" >> "$TOOL_INSTALL_LOG" 2>&1
BUILD_EXIT=$?
set -eo pipefail
if [ $BUILD_EXIT -ne 0 ]; then
    echo -e "${RED}Error: failed to build slife from source.${NC}"
    echo -e "${YELLOW}Last lines of build log:${NC}"
    tail -n 20 "$TOOL_INSTALL_LOG"
    echo -e "${YELLOW}Help: $SLIFE_REPO${NC}"
    exit 1
fi

set +eo pipefail
uv tool install --python 3.13 --find-links "$WHEELHOUSE" slife > "$TOOL_INSTALL_LOG" 2>&1
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

        # llama-cpp-python is env-specific and version-locked in the README
        # (==0.3.34).  The name-only restore resolves the newest version and
        # silently drifts from the lock — pin the README version.  On
        # Linux/WSL/macOS it then compiles from the PyPI sdist (the standard
        # build; needs a C compiler + CMake); Windows uses the upstream
        # prebuilt wheel instead (no default MSVC).
        if grep -qi '^llama-cpp-python' "$EXTRA_REQS" 2>/dev/null; then
            echo -e "  ${YELLOW}llama-cpp-python: pinning to the README lock ==0.3.34 (source-compiled from PyPI)${NC}"
            sed -i 's/^llama-cpp-python.*/llama-cpp-python==0.3.34/' "$EXTRA_REQS"
        fi

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
# silently; an existing one is only replaced (after a per-file "yes") when its
# content differs from the bundled default.
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
        # Same content as the bundled default — nothing to do.
        if cmp -s "$_src" "$_target" 2>/dev/null; then
            echo -e "  ${GRAY}unchanged  $_target${NC}"
            continue
        fi
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

# Skills: copy the bundled skills into ~/.slife/skills/.  A skill that
# doesn't exist yet is copied as-is; an existing skill of the SAME NAME (the
# user may have edited it) is only replaced after a per-skill confirm.
SKILLS_SRC="$SEED_DIR/skills"
SKILLS_DST="$HOME/.slife/skills"
if [ -d "$SKILLS_SRC" ]; then
    echo -e "${YELLOW}[4c] Setting up skills (bundled defaults)…${NC}"
    mkdir -p "$SKILLS_DST" 2>/dev/null || true
    for _skill in "$SKILLS_SRC"/*/; do
        [ -d "$_skill" ] || continue
        _name="$(basename "$_skill")"
        _dst="$SKILLS_DST/$_name"
        if [ -d "$_dst" ]; then
            # Same content as the bundled default — nothing to do.
            if diff -rq "$_skill" "$_dst" >/dev/null 2>&1; then
                echo -e "  ${GRAY}unchanged  skill '$SKILLS_DST/$_name'${NC}"
                continue
            fi
            _ask="n"
            if [ -t 0 ]; then
                read -p "  Overwrite skill '$SKILLS_DST/$_name' with the bundled default? (y/N, default: N): " _ask
            fi
            if [ "$_ask" = "y" ] || [ "$_ask" = "Y" ]; then
                rm -rf "$_dst" 2>/dev/null || true
                if cp -R "$_skill" "$_dst" 2>/dev/null; then
                    echo -e "  ${GRAY}overwrote skill '$SKILLS_DST/$_name'${NC}"
                else
                    echo -e "  ${RED}⚠ could not write skill '$SKILLS_DST/$_name'${NC}"
                fi
            fi
        else
            if cp -R "$_skill" "$_dst" 2>/dev/null; then
                echo -e "  ${GRAY}seeded skill '$SKILLS_DST/$_name'${NC}"
            else
                echo -e "  ${RED}⚠ could not write skill '$SKILLS_DST/$_name'${NC}"
            fi
        fi
    done
fi

#
# ── Semantic memory search — NOT installed by default ────────────────────
# The embedding backend is env-specific (CPU / CUDA / Metal) and the model
# download is ~2 GB, so the installer does not do it.  The user sets it up
# from the README (per-environment commands).  Keyword search already works.
echo -e "${YELLOW}[4d] Semantic memory search (optional, not installed by default)…${NC}"
echo -e "${GRAY}  Keyword search works now.  For semantic/hybrid search, set up the embedding backend +${NC}"
echo -e "${GRAY}  model yourself — see README.md → Install → Semantic memory search.${NC}"

#
# ── MCP server catalog — build it now so the index is ready ─────────────
# Bounded: a first-run build spawns every configured npx/uvx server (cold
# package downloads) and can take minutes — it must never hang the install.
echo -e "${YELLOW}[4e] Building the MCP server catalog (mcp-plugin build, max 180s)…${NC}"
MCP_PLUGIN_BIN="$(uv tool dir 2>/dev/null)/slife/bin/mcp-plugin"
if [ -x "$MCP_PLUGIN_BIN" ]; then
    set +eo pipefail
    if command -v timeout &>/dev/null; then
        # SIGINT (not SIGTERM) so the build's finally block tears the pool
        # down cleanly (kills spawned npx/uvx); --kill-after bounds a hang
        # that ignores INT.  180s covers the cold npx/uvx first run.
        timeout --signal=INT --kill-after=15 180 "$MCP_PLUGIN_BIN" build >> "$TOOL_INSTALL_LOG" 2>&1
    else
        "$MCP_PLUGIN_BIN" build >> "$TOOL_INSTALL_LOG" 2>&1
    fi
    BUILD_RC=$?
    set -eo pipefail
    if [ "$BUILD_RC" -eq 0 ]; then
        echo -e "${GREEN}  ✓${NC} MCP server catalog ready"
    elif [ "$BUILD_RC" -eq 124 ] || [ "$BUILD_RC" -eq 130 ]; then
        echo -e "${YELLOW}  ⚠ mcp-plugin build deferred (timeout/interrupt) — non-fatal;${NC}"
        echo -e "${YELLOW}    run 'mcp-plugin build' later to finish the catalog${NC}"
    else
        echo -e "${YELLOW}  ⚠ mcp-plugin build had issues (non-fatal) — log tail:${NC}"
        tail -10 "$TOOL_INSTALL_LOG" | while IFS= read -r _l; do echo -e "    ${GRAY}$_l${NC}"; done
    fi
else
    echo -e "${YELLOW}  ⚠ mcp-plugin not found — catalog build skipped${NC}"
fi

#
echo -e "${YELLOW}[5/5] Cleaning up previous installation artifacts…${NC}"

# Ensure ~/.local/bin and the slife tool venv bin (mcp-plugin / local-embed)
# are on PATH (uv puts tool executables here, and Step 1 only added the former
# to the current session).  The tool bin entry is what makes the documented
# `mcp-plugin build` / `local-embed set-gguf` commands work out-of-the-box.
SLIFE_TOOL_BIN="$(uv tool dir)/slife/bin"
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile" "$HOME/.config/fish/config.fish"; do
    if [ -f "$rc" ]; then
        if ! grep -qF "$HOME/.local/bin" "$rc" 2>/dev/null; then
            if echo "$rc" | grep -q fish; then
                echo "fish_add_path $HOME/.local/bin" >> "$rc"
            else
                echo "export PATH=\"$HOME/.local/bin:\$PATH\"" >> "$rc"
            fi
        fi
        if ! grep -qF "$SLIFE_TOOL_BIN" "$rc" 2>/dev/null; then
            if echo "$rc" | grep -q fish; then
                echo "fish_add_path $SLIFE_TOOL_BIN" >> "$rc"
            else
                echo "export PATH=\"$SLIFE_TOOL_BIN:\$PATH\"" >> "$rc"
            fi
        fi
    fi
done
export PATH="$HOME/.local/bin:$SLIFE_TOOL_BIN:$PATH"

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
    echo -e "${CYAN}Core install done${NC} — external MCP servers (catalog built), Mosquitto"
    echo "  (yt-dlp / browser-harness skipped — add later: uv tool install --python 3.12 browser-harness)"
else
    echo -e "${CYAN}Installed${NC} — slife + credstore, external MCP servers (catalog built), Mosquitto, yt-dlp, browser-harness"
fi
echo "  Semantic memory search: not installed by default — see README.md → Install → Semantic memory search"
echo ""
echo -e "${CYAN}More info:${NC} $SLIFE_REPO"
