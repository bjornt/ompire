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
# --status resolves a browser without touching anything: it reports the binary
# it would use, its version, and where it came from, exiting non-zero when
# there is none. That is the one command an agent runs before deciding whether
# it can produce browser evidence.
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
status=0

usage() {
  cat <<'EOF'
Usage: scripts/setup-browser.sh [options]

  --version=X.Y.Z.W   Chrome for Testing build to seed (default: auto-detect)
  --cache-dir=DIR     Puppeteer cache root (default: $PI_CONFIG_DIR/puppeteer)
  --skip-deps         Do not apt-get install runtime dependencies
  --skip-seed         Install deps only; let Puppeteer download Chrome itself
  --write-env         Pin PUPPETEER_EXECUTABLE_PATH in $PI_CONFIG_DIR/.env
  --status            Report the browser that would be used, then exit.
                      Installs and downloads nothing. Exit 0 when one is
                      usable, non-zero with a reason when none is.
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
    --status)      status=1 ;;
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

# ---- 6. read-only status -------------------------------------------------

# Where a usable Chrome comes from, in the order a caller should prefer:
# an explicit override, the workshop SDK, the seeded Puppeteer cache, then
# whatever the host has on PATH. Prints "<path><tab><source>", empty when
# nothing resolves. It runs in a subshell, so it reports rather than exports.
resolve_browser() {
  local bin="" source="" candidate sdk name

  if [ -n "${PUPPETEER_EXECUTABLE_PATH:-}" ] && [ -x "${PUPPETEER_EXECUTABLE_PATH}" ]; then
    bin="$PUPPETEER_EXECUTABLE_PATH"
    source="PUPPETEER_EXECUTABLE_PATH"
  elif command -v pptr-node >/dev/null 2>&1; then
    sdk="$(cd "$(dirname "$(readlink -f "$(command -v pptr-node)")")/.." && pwd)"
    if [ -x "$sdk/chrome/chrome" ]; then
      bin="$sdk/chrome/chrome"
      source="workshop SDK, via pptr-node"
    fi
  fi

  if [ -z "$bin" ]; then
    candidate="$(detect_cached_version)"
    if [ -n "$candidate" ]; then
      candidate="$cache_dir/chrome/linux-$candidate/chrome-linux64/chrome"
      if [ -x "$candidate" ]; then
        bin="$candidate"
        source="seeded Puppeteer cache in $cache_dir"
      fi
    fi
  fi

  if [ -z "$bin" ]; then
    for name in google-chrome-stable google-chrome chromium chromium-browser; do
      if command -v "$name" >/dev/null 2>&1; then
        bin="$(command -v "$name")"
        source="$name on PATH"
        break
      fi
    done
  fi

  [ -z "$bin" ] || printf '%s\t%s' "$bin" "$source"
}

report_status() {
  local bin source missing version
  IFS=$'\t' read -r bin source < <(resolve_browser) || true

  if [ -z "${bin:-}" ]; then
    {
      echo "setup-browser: no usable browser found"
      echo "  looked at: \$PUPPETEER_EXECUTABLE_PATH, pptr-node on PATH,"
      echo "             $cache_dir, then chrome/chromium on PATH"
      echo "  provision one with: scripts/setup-browser.sh"
    } >&2
    return 1
  fi

  missing="$(ldd "$bin" 2>/dev/null | awk '/not found/{print $1}')" || true
  if [ -n "$missing" ]; then
    {
      echo "setup-browser: $bin has unresolved libraries:"
      printf '  %s\n' "$missing"
      echo "  install them with: scripts/setup-browser.sh --skip-seed"
    } >&2
    return 1
  fi

  if ! version="$("$bin" --version 2>&1)"; then
    echo "setup-browser: $bin is not runnable: $version" >&2
    return 1
  fi
  version="$(printf '%s' "$version" | head -1 | sed 's/[[:space:]]*$//')"

  # A path can point at anything; only a binary that identifies itself as
  # Chrome or Chromium is one Puppeteer can drive.
  case "$version" in
    *Chrom*) ;;
    *)
      {
        echo "setup-browser: $bin does not identify as Chrome or Chromium"
        echo "  it reported: $version"
      } >&2
      return 1
      ;;
  esac

  printf 'browser: available\n'
  printf 'binary:  %s\n' "$bin"
  printf 'version: %s\n' "$version"
  printf 'source:  %s\n' "$source"
}

# ---- main ----------------------------------------------------------------

if [ "$status" -eq 1 ]; then
  if report_status; then exit 0; else exit 1; fi
fi

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
