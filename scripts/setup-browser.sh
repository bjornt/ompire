#!/usr/bin/env bash
#
# Provision a headless Chromium for the oh-my-pi `browser` tool (Puppeteer).
#
# The browser tool drives Chrome for Testing through Puppeteer. On a fresh
# container two things are missing and Puppeteer's own bootstrap dies on both:
#
#   1. an unzip tool  -- Puppeteer downloads chrome-linux64.zip but its
#                        extractor refuses to run without `unzip` on PATH, so
#                        the download lands but never unpacks.
#   2. Chrome's runtime shared libraries (libnss3, libatk*, libgbm1, ...) --
#                        without them the extracted binary fails to exec with
#                        "error while loading shared libraries: ...".
#
# Installing those (see DEPS below) is the whole requirement: after that the
# browser tool self-provisions -- Puppeteer fetches Chrome from Google's
# Chrome-for-Testing bucket (~180 MB) and launches it.
#
# This script also optionally *pre-seeds* the exact Chrome build into the
# Puppeteer cache so the first browser call does no cold-start download. The
# cache layout Puppeteer resolves against is:
#
#     <cache>/chrome/linux-<version>/chrome-linux64/chrome
#
# where <cache> is $PI_CONFIG_DIR/puppeteer (default ~/.omp/puppeteer). Drop a
# matching binary there and Puppeteer uses it verbatim.
#
# Version selection (first hit wins):
#   --version=X.Y.Z.W          explicit override
#   newest existing linux-*    the version this omp build already resolved
#   DEFAULT_VERSION            the pinned fallback below
#
# With --write-env the resolved binary is also pinned via
# PUPPETEER_EXECUTABLE_PATH in $PI_CONFIG_DIR/.env (loaded on every omp start),
# which makes the browser tool use exactly this binary and skip Puppeteer's
# download/version logic entirely -- the air-gapped / never-re-download option.
#
# No third-party dependencies: bash, apt-get, and curl or wget.

set -euo pipefail

# Chrome for Testing build to seed when no version can be inferred from the
# cache. Bump this when omp bumps its pinned Chrome.
DEFAULT_VERSION="150.0.7871.24"

# Runtime dependencies. `unzip` unblocks Puppeteer's extractor; the rest are
# the non-base shared libraries Chrome links (verified via `ldd` on the
# Chrome-for-Testing binary on Ubuntu 24.04). Headless Chrome needs no
# libgtk-3 / X server, so those are intentionally absent.
DEPS=(
  ca-certificates
  unzip
  libnss3 libnspr4
  libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64
  libcups2t64 libdrm2 libgbm1 libxkbcommon0
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2
  libasound2t64 libpango-1.0-0 libcairo2 fonts-liberation
)

CDN="https://storage.googleapis.com/chrome-for-testing-public"

# ---- options -------------------------------------------------------------

version=""
cache_dir="${PI_CONFIG_DIR:-$HOME/.omp}/puppeteer"
skip_deps=0
skip_seed=0
write_env=0

usage() {
  cat <<'EOF'
Usage: scripts/setup-browser.sh [options]

  --version=X.Y.Z.W   Chrome for Testing build to seed (default: auto-detect)
  --cache-dir=DIR     Puppeteer cache root (default: $PI_CONFIG_DIR/puppeteer)
  --skip-deps         Do not apt-get install runtime dependencies
  --skip-seed         Install deps only; let Puppeteer download Chrome itself
  --write-env         Pin PUPPETEER_EXECUTABLE_PATH in $PI_CONFIG_DIR/.env
  -h, --help          Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --version=*)   version="${arg#*=}" ;;
    --cache-dir=*) cache_dir="${arg#*=}" ;;
    --skip-deps)   skip_deps=1 ;;
    --skip-seed)   skip_seed=1 ;;
    --write-env)   write_env=1 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "setup-browser: unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

log() { printf '==> %s\n' "$*"; }

# ---- 1. runtime dependencies --------------------------------------------

install_deps() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "setup-browser: apt-get not found; install these manually: ${DEPS[*]}" >&2
    exit 1
  fi

  local sudo=""
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || {
      echo "setup-browser: need root or sudo to apt-get install" >&2; exit 1; }
    sudo="sudo"
  fi

  log "installing runtime dependencies (${#DEPS[@]} packages)"
  DEBIAN_FRONTEND=noninteractive $sudo apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $sudo apt-get install -y -qq "${DEPS[@]}"
}

# ---- 2. resolve the Chrome version to seed ------------------------------

# Newest linux-<version> directory already present in the cache, if any. That
# is the version this omp build resolved, so matching it means Puppeteer finds
# our seed instead of downloading.
detect_cached_version() {
  local d name best=""
  for d in "$cache_dir"/chrome/linux-*; do
    [ -d "$d" ] || continue
    name="${d##*/linux-}"
    if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$best" "$name" | sort -V | tail -1)" = "$name" ]; then
      best="$name"
    fi
  done
  printf '%s' "$best"
}

resolve_version() {
  if [ -n "$version" ]; then printf '%s' "$version"; return; fi
  local cached; cached="$(detect_cached_version)"
  if [ -n "$cached" ]; then printf '%s' "$cached"; return; fi
  printf '%s' "$DEFAULT_VERSION"
}

# ---- 3. seed Chrome into the Puppeteer cache ----------------------------

fetch() {
  local url="$1" out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fSL --retry 3 "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$out"
  else
    echo "setup-browser: need curl or wget to download Chrome" >&2; exit 1
  fi
}

seed_chrome() {
  local v="$1"
  local dest="$cache_dir/chrome/linux-$v"
  local bin="$dest/chrome-linux64/chrome"

  if [ -x "$bin" ] && "$bin" --version >/dev/null 2>&1; then
    log "Chrome $v already present and runnable at $bin"
    CHROME_BIN="$bin"
    return
  fi

  local url="$CDN/$v/linux64/chrome-linux64.zip"
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  log "downloading Chrome for Testing $v"
  fetch "$url" "$tmp/chrome.zip"

  log "extracting into $dest"
  mkdir -p "$dest"
  unzip -q -o "$tmp/chrome.zip" -d "$dest"

  [ -x "$bin" ] || { echo "setup-browser: expected binary missing at $bin" >&2; exit 1; }
  CHROME_BIN="$bin"
}

# ---- 4. verify the binary actually launches -----------------------------

verify_chrome() {
  local bin="$1"
  local missing
  missing="$(ldd "$bin" 2>/dev/null | awk '/not found/{print $1}')" || true
  if [ -n "$missing" ]; then
    echo "setup-browser: Chrome has unresolved libraries:" >&2
    printf '  %s\n' "$missing" >&2
    echo "install the missing packages (or drop --skip-deps) and re-run." >&2
    exit 1
  fi
  log "verified: $("$bin" --version)"
}

# ---- 5. optionally pin PUPPETEER_EXECUTABLE_PATH ------------------------

write_env_override() {
  local bin="$1"
  local env_file="${PI_CONFIG_DIR:-$HOME/.omp}/.env"
  local key="PUPPETEER_EXECUTABLE_PATH"

  mkdir -p "$(dirname "$env_file")"
  touch "$env_file"
  # Drop any prior line for this key, then append the fresh one.
  local tmp; tmp="$(mktemp)"
  grep -v "^${key}=" "$env_file" > "$tmp" || true
  printf '%s=%s\n' "$key" "$bin" >> "$tmp"
  mv "$tmp" "$env_file"
  log "pinned $key=$bin in $env_file"
}

# ---- main ----------------------------------------------------------------

[ "$skip_deps" -eq 1 ] || install_deps

if [ "$skip_seed" -eq 1 ]; then
  log "skipping Chrome seed; Puppeteer will download on first browser call"
  exit 0
fi

v="$(resolve_version)"
CHROME_BIN=""
seed_chrome "$v"
verify_chrome "$CHROME_BIN"
[ "$write_env" -eq 0 ] || write_env_override "$CHROME_BIN"

log "browser tool ready ($CHROME_BIN)"
