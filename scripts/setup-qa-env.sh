#!/usr/bin/env bash
#
# Set up everything needed to dogfood-QA ompire with the AI agent identity
# provisioned by setup-qa-agent.sh. Run this on the trusted QA host AFTER the
# identity directory has been created and placed at .qa-agent/ in this repo.
# Never run provisioning inside a task sandbox.
#
# What it does, in order:
#
#   1. preflight    verify .qa-agent/ is complete (token, ssh key, gnupg,
#                   env.sh, state.json) and source env.sh — every git/gh/gpg
#                   operation from here on runs as the bot (@ompire-test)
#   2. gitignore    add .qa-agent/ to .gitignore — it contains a live PAT,
#                   an SSH private key, and a GPG private key
#   3. gh           install the GitHub CLI for trusted daemon publishing
#                   (GH_TOKEN from env.sh; no management-plane login here)
#   3b. toolchain   node (NodeSource 24) + corepack pnpm (pinned in
#                   frontend/package.json#packageManager), uv (astral installer)
#   3c. workshop    workshop snap + LXD + my-workshop (built from
#                   github.com/bjornt/my-workshop) — the daemon spawns task
#                   agents through them. Skip with --skip-workshop.
#   4. browser      delegate to setup-browser.sh (UI dogfooding via the
#                   omp browser tool); skip with --skip-browser
#   5. build        pnpm install + production build (frontend; the daemon
#                   serves frontend/dist), uv sync (daemon)
#   6. config       ~/.config/ompire/config.toml: gpg_signing_key = the bot's
#                   key, notifications_enabled = false (headless QA env)
#   7. qa repo      resolve the QA sandbox repo (auto-discovered via the PAT —
#                   a fine-grained PAT sees only its selected repos — or pass
#                   --repo owner/name), clone it to ~/proj/<name> over SSH
#   8. daemon       write ~/.config/ompire/qa-daemon.sh (sources env.sh, then
#                   uv run ompire-daemon) and (re)start it; the daemon needs
#                   the bot env for ship-flow git/gh/gpg operations
#  10. smoke        verify: PAT and daemon GitHub identity, registered sandbox
#                   target eligibility, ssh, GPG probe, and UI serving — no
#                   credential value is printed
#
# Options:
#   --probe             read-only readiness check; exclusive with all options
#   --repo OWNER/NAME   QA sandbox repo (default: auto-discover from the PAT)
#   --skip-browser      skip the Chromium provisioning step
#   --skip-workshop     skip the workshop/LXD/my-workshop provisioning step
#   --check             additionally run the daemon + frontend test suites
#   --dir PATH          identity directory (default: <repo>/.qa-agent)
#
# Notes:
#   - The daemon keeps running afterwards (nohup; logs + pid in
#     ~/.local/share/ompire/). Restart later with ~/.config/ompire/qa-daemon.sh.
#   - Re-running is safe: every step is idempotent.
#   - The bot's GPG key is passphrase-protected and the daemon wrapper warms
#     gpg-agent at startup, so the ship gate sees it as "ready". A cold key
#     reports "locked" instead; the smoke step reports the daemon's own
#     verdict either way.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
QA_DIR=${QA_AGENT_DIR:-"$ROOT/.qa-agent"}


OMPIRE_CONFIG_DIR="$HOME/.config/ompire"
OMPIRE_CONFIG="$OMPIRE_CONFIG_DIR/config.toml"
OMPIRE_DATA="$HOME/.local/share/ompire"
DAEMON_WRAPPER="$OMPIRE_CONFIG_DIR/qa-daemon.sh"
BROKER_WRAPPER="$OMPIRE_CONFIG_DIR/qa-forge-broker.sh"
BROKER_PIDFILE="$OMPIRE_DATA/forge-broker.pid"
BROKER_LOG="$OMPIRE_DATA/forge-broker.log"
DAEMON_URL="http://127.0.0.1:4173"
CHECKOUT_ROOT="$HOME/proj"

OPT_REPO=""
OPT_SKIP_BROWSER=0
OPT_SKIP_WORKSHOP=0
OPT_CHECK=0

usage() {
	sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d' | sed '$d'
}

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
probe() {
	local failed=0 f expected actual tool
	probe_fail() { printf 'dogfood: %s\n' "$*" >&2; failed=1; }
	for f in . gnupg token .curlrc env.sh id_ed25519 gitconfig known_hosts state.json .gpg-passphrase; do
		expected=600
		case "$f" in . | gnupg) expected=700 ;; known_hosts) expected=644 ;; esac
		if { [ "$expected" = 700 ] && [ ! -d "$QA_DIR/$f" ]; } ||
			{ [ "$expected" != 700 ] && [ ! -f "$QA_DIR/$f" ]; }; then
			probe_fail "identity $f missing — re-copy the identity with setup-qa-agent.sh on the management machine"
			continue
		fi
		actual=$(stat -c %a "$QA_DIR/$f" 2>/dev/null) || actual=unknown
		if [ "$actual" != "$expected" ] || [ ! -O "$QA_DIR/$f" ] || [ ! -r "$QA_DIR/$f" ]; then
			probe_fail "identity $f needs owner access and mode $expected — fix ownership and chmod $expected on the trusted QA host"
		fi
	done
	if ! curl -q -fsS --noproxy '*' --connect-timeout 1 --max-time 2 \
		http://127.0.0.1:4100/healthz >/dev/null 2>&1; then
		probe_fail "forge broker unavailable on localhost:4100 — restart $BROKER_WRAPPER"
	fi
	if ! timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/4000' >/dev/null 2>&1; then
		probe_fail "auth gateway unavailable on localhost:4000 — start scripts/qa-auth-tunnel.sh from the gateway host"
	fi
	if ! timeout 10 "$ROOT/scripts/setup-browser.sh" --status >/dev/null 2>&1; then
		probe_fail "browser unavailable — run scripts/setup-browser.sh on the trusted QA host"
	fi
	for tool in workshop my-workshop lxc; do
		command -v "$tool" >/dev/null 2>&1 ||
			probe_fail "container tooling $tool missing — re-run scripts/setup-qa-env.sh without --skip-workshop"
	done
	return "$failed"
}

# ---- options -------------------------------------------------------------
# Do not source env.sh, read credentials, or provision anything on this path.
for arg in "$@"; do
	if [ "$arg" = --probe ]; then
		[ "$#" -eq 1 ] || die "--probe is mutually exclusive with all other options"
		probe
		exit $?
	fi
done

while [ $# -gt 0 ]; do
	case "$1" in
	--probe) die "--probe is mutually exclusive with all other options" ;;
	--repo) [ $# -ge 2 ] || die "--repo needs OWNER/NAME"; OPT_REPO=$2; shift 2 ;;
	--dir) [ $# -ge 2 ] || die "--dir needs PATH"; QA_DIR=$2; shift 2 ;;
	--skip-browser) OPT_SKIP_BROWSER=1; shift ;;
	--skip-workshop) OPT_SKIP_WORKSHOP=1; shift ;;
	--check) OPT_CHECK=1; shift ;;
	help | --help | -h) usage; exit 0 ;;
	*) die "unknown argument: $1 (try 'help')" ;;
	esac
done

# ---- 1. preflight: the identity must already exist ------------------------
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' ||
	die "Python 3.12 or newer is required — install it on the trusted QA host"


log "preflight: checking QA identity in $QA_DIR"
for f in token .curlrc env.sh id_ed25519 gitconfig known_hosts state.json; do
	[ -f "$QA_DIR/$f" ] || die "missing $QA_DIR/$f — run setup-qa-agent.sh setup first"
done
[ -d "$QA_DIR/gnupg" ] || die "missing $QA_DIR/gnupg — run setup-qa-agent.sh setup first"

BOT_LOGIN=$(jq -r '.login // empty' "$QA_DIR/state.json")
BOT_FPR=$(jq -r '.gpg.fingerprint // empty' "$QA_DIR/state.json")
[ -n "$BOT_LOGIN" ] || die "state.json has no login — re-run setup-qa-agent.sh setup"
[ -n "$BOT_FPR" ] || die "state.json has no gpg fingerprint — re-run setup-qa-agent.sh setup"
[ "$BOT_LOGIN" = ompire-test ] || die "QA requires @ompire-test — never provision with a personal identity"
QA_DIR=$(cd "$QA_DIR" && pwd)

# From here on, act as the bot: git/gh/gpg all pick up the identity.
# shellcheck disable=SC1090
. "$QA_DIR/env.sh"
[ -n "${GH_TOKEN:-}" ] || die "env.sh produced an empty GH_TOKEN — is $QA_DIR/token readable?"
[ -d "${GNUPGHOME:-/nonexistent}" ] || die "GNUPGHOME ($GNUPGHOME) does not exist — stale absolute paths? regenerate env.sh with setup-qa-agent.sh"
[ -f "${GIT_CONFIG_GLOBAL:-/nonexistent}" ] || die "GIT_CONFIG_GLOBAL ($GIT_CONFIG_GLOBAL) does not exist"
log "identity: @$BOT_LOGIN (gpg $BOT_FPR)"

# ---- 2. gitignore: never commit the identity -----------------------------

if ! grep -qxF '.qa-agent/' "$ROOT/.gitignore" 2>/dev/null; then
	printf '.qa-agent/\n' >>"$ROOT/.gitignore"
	log "gitignore: added .qa-agent/ (contains live credentials)"
fi

# ---- 3. gh CLI ------------------------------------------------------------

if command -v gh >/dev/null; then
	log "gh: already installed ($(gh --version | head -1))"
else
	log "gh: installing"
	if sudo -n true 2>/dev/null; then
		sudo install -d -m 755 /etc/apt/keyrings
		curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg |
			sudo dd of=/etc/apt/keyrings/githubcli-archive-keyring.gpg status=none
		printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' \
			"$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
		sudo apt-get update -qq
		sudo DEBIAN_FRONTEND=noninteractive apt-get install -y gh
	else
		# rootless fallback: latest release tarball into ~/.local/bin
		ver=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | jq -r '.tag_name')
		[ -n "$ver" ] || die "could not resolve latest gh release"
		tmp=$(mktemp -d)
		curl -fsSL "https://github.com/cli/cli/releases/download/$ver/gh_${ver#v}_linux_amd64.tar.gz" |
			tar xz -C "$tmp"
		mkdir -p "$HOME/.local/bin"
		install -m 755 "$tmp"/gh_*/bin/gh "$HOME/.local/bin/gh"
		rm -rf "$tmp"
		case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to PATH" ;; esac
	fi
	log "gh: installed ($(gh --version | head -1))"
fi

# ---- 3b. toolchain: node + pnpm + uv ---------------------------------------

if ! command -v node >/dev/null; then
	log "node: installing (NodeSource 24)"
	curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - >/dev/null
	sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs >/dev/null
fi
# The frontend pins pnpm in package.json#packageManager; corepack downloads
# and uses the exact version declared there. Remove any stale global install
# so the corepack shim wins in PATH.
sudo rm -f /usr/bin/pnpm /usr/bin/pnpx /usr/local/bin/pnpm /usr/local/bin/pnpx
sudo corepack enable pnpm
hash -r
# openspec is a global tool, not a project dependency
if ! command -v openspec >/dev/null; then
	log "openspec: installing globally via npm"
	sudo npm install -g @fission-ai/openspec >/dev/null
fi
if ! command -v uv >/dev/null; then
	log "uv: installing (astral installer)"
	curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
	export PATH="$HOME/.local/bin:$PATH"
fi
log "toolchain: node $(node --version), pnpm $(cd "$ROOT/frontend" && pnpm --version), uv $(uv --version | awk '{print $2}')"

# ---- 3c. workshop stack (host-level: the daemon spawns agents through it) --

if [ "$OPT_SKIP_WORKSHOP" -eq 1 ]; then
	log "workshop: skipped (--skip-workshop)"
else
	if ! command -v workshop >/dev/null; then
		log "workshop: installing snap + LXD"
		sudo snap install workshop --classic
		# workshop requires LXD >= 6.8; Ubuntu's default channel serves 5.21.x
		sudo snap install lxd --channel=6/stable
		sudo lxd init --auto
		sudo usermod -aG lxd "$USER"
		warn "added $USER to group lxd — takes effect on next login; start a new
          login shell before spawning tasks, or workshop launches will fail"
	fi
	if ! command -v my-workshop >/dev/null; then
		log "my-workshop: building from github.com/bjornt/my-workshop"
		command -v go >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y golang-go >/dev/null
		mw=$(mktemp -d)
		git clone --quiet --depth 1 https://github.com/bjornt/my-workshop "$mw/src"
		( cd "$mw/src" && go build -o my-workshop ./cmd/my-workshop )
		sudo install -m 755 "$mw/src/my-workshop" /usr/local/bin/my-workshop
		rm -rf "$mw"
	fi
	log "workshop: $(workshop --version 2>/dev/null || echo present), my-workshop present"
fi

# ---- 4. browser -----------------------------------------------------------

if [ "$OPT_SKIP_BROWSER" -eq 1 ]; then
	log "browser: skipped (--skip-browser)"
else
	"$ROOT/scripts/setup-browser.sh"
fi

# ---- 5. dependencies + build ----------------------------------------------

# Run pnpm with frontend/ as the working directory, never `pnpm -C frontend`
# from the root: corepack resolves the pinned version from the CWD's
# package.json, and the repository root deliberately has none, so -C silently
# runs corepack's fallback version against the project's pin.
log "deps: pnpm install (frontend), uv sync (daemon)"
(cd "$ROOT/frontend" && pnpm install --frozen-lockfile)
uv sync --project "$ROOT/daemon" --quiet

log "build: frontend production bundle (daemon serves frontend/dist)"
(cd "$ROOT/frontend" && pnpm build)

if [ "$OPT_CHECK" -eq 1 ]; then
	log "check: daemon + frontend test suites"
	(cd "$ROOT/daemon" && uv run pytest -q)
	(cd "$ROOT/frontend" && pnpm test)
fi

# ---- 6. trusted-side directories ------------------------------------------

mkdir -p "$OMPIRE_CONFIG_DIR" "$OMPIRE_DATA"

# ---- 7. QA sandbox repo ---------------------------------------------------

if [ -n "$OPT_REPO" ]; then
	REPO=$OPT_REPO
else
	log "repo: auto-discovering via the PAT's repository selection"
	REPO_LIST=$(curl -fsS -K "$QA_DIR/.curlrc" "https://api.github.com/user/repos?per_page=100" |
		jq -r '.[].full_name') || die "repo discovery failed — pass --repo owner/name"
	count=$(grep -c . <<<"$REPO_LIST" || true)
	case "$count" in
	1) REPO=$REPO_LIST ;;
	0) die "the PAT sees no repos — pass --repo owner/name (and fix the PAT's repository selection)" ;;
	*) die "the PAT sees multiple repos — pick one with --repo:
$REPO_LIST" ;;
	esac
fi
log "repo: $REPO"
[[ "$REPO" =~ ^ompire-test/[A-Za-z0-9_.-]+$ ]] ||
	die "QA repository must be owned by ompire-test — never target a real repository"

repo_json=$(curl -fsS -K "$QA_DIR/.curlrc" "https://api.github.com/repos/$REPO") ||
	die "cannot read repo $REPO with the agent PAT"
BASE_BRANCH=$(jq -r '.default_branch // "main"' <<<"$repo_json")
SSH_URL=$(jq -r '.ssh_url // empty' <<<"$repo_json")
[ -n "$SSH_URL" ] || die "repo $REPO has no ssh_url"

NAME=$(basename "$REPO" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-' | sed 's/--*/-/g; s/^-//; s/-$//')
[ "$NAME" != ompire ] || die "sandbox project slug ompire is reserved for the dogfood checkout — rename the sandbox repository"
[ -n "$NAME" ] || die "could not derive a project slug from $REPO"
CHECKOUT="$CHECKOUT_ROOT/$NAME"

if [ -d "$CHECKOUT/.git" ]; then
	existing=$(git -C "$CHECKOUT" remote get-url origin 2>/dev/null || true)
	[ "$existing" = "$SSH_URL" ] || die "checkout $CHECKOUT exists but origin is '$existing' (expected $SSH_URL)"
	log "checkout: $CHECKOUT already present"
else
	mkdir -p "$CHECKOUT_ROOT"
	log "checkout: cloning $SSH_URL -> $CHECKOUT"
	git clone "$SSH_URL" "$CHECKOUT"
fi

# An empty repo has no base branch and task clones/ship flow need one — seed
# it. Doubles as a live test of the whole identity chain: bot git identity,
# GPG-signed commit, ssh push.
if ! git -C "$CHECKOUT" rev-parse --verify HEAD >/dev/null 2>&1; then
	log "seed: empty repo — pushing initial GPG-signed commit to $BASE_BRANCH"
	(
		cd "$CHECKOUT"
		git checkout -q -B "$BASE_BRANCH"
		printf '# %s\n\nQA sandbox for ompire dogfooding.\n' "$REPO" >README.md
		git add README.md
		git commit -q -m "chore: initial commit (QA sandbox seed)"
		git push -q -u origin "$BASE_BRANCH"
	)
fi

# ---- 7b. dogfood environment configuration --------------------------------

python3 - "$OMPIRE_CONFIG" "$ROOT/scripts/setup-qa-env.sh" "$QA_DIR" "$REPO" "$BOT_FPR" <<'PY'
import json
import pathlib
import re
import sys
import tomllib

path = pathlib.Path(sys.argv[1])
script, identity, repo, fingerprint = sys.argv[2:]
text = path.read_text() if path.exists() else (
    f'gpg_signing_key = {json.dumps(fingerprint)}\nnotifications_enabled = false\n'
)
original = tomllib.loads(text)
headers = []
for match in re.finditer(r'^[ \t]*\[.+?\][ \t]*(?:#[^\n]*)?$', text, re.MULTILINE):
    try:
        # A header-looking line inside a multiline string is not a table.
        tomllib.loads(text[:match.start()])
        table = tomllib.loads(match.group())
    except tomllib.TOMLDecodeError:
        continue
    keys = []
    while isinstance(table, dict) and len(table) == 1:
        key, table = next(iter(table.items()))
        keys.append(key)
    headers.append((match.start(), keys[:2] == ['environments', 'dogfood']))
if 'dogfood' in original.get('environments', {}) and not any(owned for _, owned in headers):
    sys.exit('dogfood config must use [environments.dogfood] tables, not an inline declaration')
headers.append((len(text), False))
parts = []
offset = 0
for index, (start, owned) in enumerate(headers[:-1]):
    if owned:
        parts.append(text[offset:start])
        offset = headers[index + 1][0]
parts.append(text[offset:])
preserved = ''.join(parts)
block = f'''[environments.dogfood]
host = "host"
provision = {json.dumps([script, '--dir', identity, '--repo', repo])}
check = {json.dumps([script, '--probe'])}
wiring = ''' + "'''\n" + '''sdks:
  - name: omp
    plugs:
      github:
        interface: tunnel
        endpoint: localhost:4100
  - name: system
    slots:
      github:
        interface: tunnel
        endpoint: localhost:4100
provision:
  connect:
    - plug: omp:github
      slot: system:github
''' + "'''\n" + '''
[environments.dogfood.capabilities]
model = "auth-gateway"
forge = "forge-broker"
browser = "chrome"
'''
updated = preserved + ('' if preserved.endswith('\n\n') else '\n' if preserved.endswith('\n') else '\n\n') + block
tomllib.loads(updated)
path.write_text(updated)
PY
log "config: $OMPIRE_CONFIG (replaced only dogfood; existing operator settings preserved)"

# ---- 7c. forge broker wrapper + (re)start ----------------------------------

{
	printf '#!/usr/bin/env bash\nset -euo pipefail\n'
	# No env.sh sourcing: the broker reads its sole credential from the file.
	printf 'unset OMPIRE_FORGE_TOKEN GH_TOKEN GITHUB_TOKEN\n'
	printf 'exec %q %q --token-file %q --repo %q --port 4100\n' \
		"$(command -v python3)" "$ROOT/daemon/src/ompire_daemon/forge_broker.py" "$QA_DIR/token" "$REPO"
} >"$BROKER_WRAPPER"
chmod 700 "$BROKER_WRAPPER"
if [ -f "$BROKER_PIDFILE" ] && kill -0 "$(cat "$BROKER_PIDFILE")" 2>/dev/null; then
	log "forge broker: restarting (pid $(cat "$BROKER_PIDFILE"))"
	kill "$(cat "$BROKER_PIDFILE")"
	for _ in $(seq 1 20); do
		kill -0 "$(cat "$BROKER_PIDFILE")" 2>/dev/null || break
		sleep 0.2
	done
	kill -0 "$(cat "$BROKER_PIDFILE")" 2>/dev/null &&
		die "forge broker did not stop — inspect $BROKER_PIDFILE before restarting"
fi
# Readiness must belong to this start, not a previous log line.
touch "$BROKER_LOG"
chmod 600 "$BROKER_LOG"
broker_start=$(wc -l <"$BROKER_LOG")
nohup "$BROKER_WRAPPER" >>"$BROKER_LOG" 2>&1 &
echo $! >"$BROKER_PIDFILE"
ready=0
for _ in $(seq 1 40); do
	kill -0 "$(cat "$BROKER_PIDFILE")" 2>/dev/null ||
		die "forge broker died during startup — see $BROKER_LOG; restart $BROKER_WRAPPER"
	if tail -n "+$((broker_start + 1))" "$BROKER_LOG" | grep -qxF 'forge broker ready http://127.0.0.1:4100' &&
		curl -q -fsS --noproxy '*' --max-time 2 http://127.0.0.1:4100/healthz >/dev/null 2>&1; then
		ready=1
		break
	fi
	sleep 0.2
done
[ "$ready" -eq 1 ] || die "forge broker not ready — see $BROKER_LOG; restart $BROKER_WRAPPER"
log "forge broker: ready (pid $(cat "$BROKER_PIDFILE"), log $BROKER_LOG)"

# ---- 8. daemon wrapper + (re)start ----------------------------------------

	cat >"$DAEMON_WRAPPER" <<-EOF
	#!/usr/bin/env bash
	# Generated by setup-qa-env.sh — starts the ompire daemon as the QA bot.
	set -euo pipefail
	. "$QA_DIR/env.sh"
	# Warm gpg-agent so the daemon's ship gate sees the signing key as cached.
	[ -f "\$QA_AGENT_DIR/.gpg-passphrase" ] &&
		gpg --batch --yes --pinentry-mode loopback \
			--passphrase-file "\$QA_AGENT_DIR/.gpg-passphrase" --clearsign <<<"warm" >/dev/null 2>&1 || true
	export PATH="\$HOME/.local/bin:/snap/bin:\$PATH"
	exec uv run --project "$ROOT/daemon" ompire-daemon
	EOF
chmod +x "$DAEMON_WRAPPER"

PIDFILE="$OMPIRE_DATA/daemon.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
	log "daemon: restarting (pid $(cat "$PIDFILE"))"
	kill "$(cat "$PIDFILE")" 2>/dev/null || true
	for _ in $(seq 1 20); do kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break; sleep 0.2; done
fi
log "daemon: starting (log: $OMPIRE_DATA/daemon.log)"
nohup "$DAEMON_WRAPPER" >>"$OMPIRE_DATA/daemon.log" 2>&1 &
echo $! >"$PIDFILE"

log "daemon: waiting for readiness on $DAEMON_URL"
ready=0
for _ in $(seq 1 60); do
	if [ -f "$OMPIRE_DATA/token" ] &&
		curl -fsS -H "Authorization: Bearer $(cat "$OMPIRE_DATA/token")" "$DAEMON_URL/api/projects" >/dev/null 2>&1; then
		ready=1
		break
	fi
	kill -0 "$(cat "$PIDFILE")" 2>/dev/null || {
		tail -20 "$OMPIRE_DATA/daemon.log" >&2
		die "daemon died during startup — see $OMPIRE_DATA/daemon.log"
	}
	sleep 0.5
done
[ "$ready" -eq 1 ] || die "daemon did not become ready — see $OMPIRE_DATA/daemon.log"
DAEMON_TOKEN=$(cat "$OMPIRE_DATA/token")

# ---- 9. register the project ----------------------------------------------

api_daemon() { # api_daemon METHOD PATH [JSON]
	local method=$1 path=$2 data=${3:-}
	local -a args=(-q -fsS --connect-timeout 2 --max-time 60 -X "$method" -H "Authorization: Bearer $DAEMON_TOKEN"
		-H "Content-Type: application/json")
	[ -n "$data" ] && args+=(-d "$data")
	curl "${args[@]}" "$DAEMON_URL$path"
}

# A launch needs a complete model profile (ADR-0026) and the QA loop runs the
# real stack, so this one must name real, provider-qualified models. It is not
# invented here: set OMPIRE_QA_MODEL_PROFILE to reuse a profile you already
# created, or OMPIRE_QA_MODEL to say which model the four roles should bind.
# There is no host-model fallback to inherit.
PROFILE=${OMPIRE_QA_MODEL_PROFILE:-qa}
QA_MODEL=${OMPIRE_QA_MODEL:-}
if api_daemon GET "/api/model-profiles/$PROFILE" >/dev/null 2>&1; then
	log "profile: '$PROFILE' already registered"
elif [ -n "$QA_MODEL" ]; then
	profile_payload=$(jq -n --arg n "$PROFILE" --arg m "$QA_MODEL" \
		'{name: $n, roles: {
		    default: {model: $m, thinking: "medium"},
		    smol:    {model: $m, thinking: "low"},
		    slow:    {model: $m, thinking: "high"},
		    plan:    {model: $m, thinking: "high"}}}')
	api_daemon POST /api/model-profiles "$profile_payload" >/dev/null ||
		die "could not create the '$PROFILE' model profile"
	log "profile: registered '$PROFILE' (all roles on $QA_MODEL)"
else
	die "no model profile '$PROFILE' exists. Create one under Settings → Model
profiles, or set OMPIRE_QA_MODEL to a provider-qualified model id (for example
anthropic/claude-sonnet-4-5) and re-run. Nothing is inferred from your omp
configuration."
fi

payload=$(jq -n --arg n "$NAME" --arg t "$REPO" --arg u "$SSH_URL" --arg c "$CHECKOUT" \
	--arg b "$BASE_BRANCH" --arg p "$PROFILE" \
	'{name: $n, title: $t, upstream_url: $u, checkout_path: $c,
	  base_branch: $b, default_model_profile: $p}')
if ! api_daemon POST /api/projects "$payload" >/dev/null 2>&1; then
	# most likely a duplicate — verify the existing registration matches
	existing=$(api_daemon GET "/api/projects/$NAME" 2>/dev/null || true)
	got=$(jq -r '.upstream_url // empty' <<<"$existing" 2>/dev/null)
	[ "$got" = "$SSH_URL" ] || die "project '$NAME' already registered with a different upstream ($got)"
	log "project: '$NAME' already registered"
else
	log "project: registered '$NAME' ($SSH_URL)"
fi
# Refresh only our local fetch remote; no real-upstream credentials are needed.
if git -C "$ROOT" remote get-url qa-self >/dev/null 2>&1; then
	git -C "$ROOT" remote set-url qa-self "$ROOT"
else
	git -C "$ROOT" remote add qa-self "$ROOT"
fi
OMPIRE_UPSTREAM=$(git -C "$ROOT" remote get-url origin) || die "ompire checkout needs an origin for project identity"
OMPIRE_BASE=$(git -C "$ROOT" symbolic-ref --short HEAD) ||
	die "ompire checkout must be on a named branch for the local qa-self fetch"
if existing=$(api_daemon GET /api/projects/ompire 2>/dev/null); then
	jq -e --arg c "$ROOT" --arg u "$OMPIRE_UPSTREAM" \
		'.checkout_path == $c and .upstream_url == $u and .fetch_remote == "qa-self"' \
		<<<"$existing" >/dev/null || die "project ompire has conflicting registration — fix its checkout/upstream/fetch_remote in Projects"
	log "project: 'ompire' already registered"
else
	payload=$(jq -n --arg u "$OMPIRE_UPSTREAM" --arg c "$ROOT" --arg b "$OMPIRE_BASE" --arg p "$PROFILE" \
		'{name: "ompire", title: "Ompire dogfood driver", upstream_url: $u,
		  checkout_path: $c, fetch_remote: "qa-self", base_branch: $b, default_model_profile: $p}')
	api_daemon POST /api/projects "$payload" >/dev/null || die "could not register the ompire project"
	log "project: registered 'ompire' (local qa-self fetch, no publish workflow)"
fi

# The library belongs to the operator. Validate our tracked source every run,
# but create an executable revision only when the name is not already owned.
workflow_payload=$(jq -n --rawfile yaml "$ROOT/scripts/qa-dogfood-workflow.yaml" \
	'{name: "dogfood", yaml: $yaml}')
api_daemon POST /api/workflow-library/validate "$workflow_payload" >/dev/null ||
	die "tracked dogfood workflow failed validation"
if api_daemon GET /api/workflow-library/dogfood >/dev/null 2>&1; then
	log "workflow: 'dogfood' already present (operator version preserved)"
else
	if created=$(api_daemon POST /api/workflow-library "$workflow_payload"); then
		version=$(jq -er '.entry.version' <<<"$created")
		revision_payload=$(jq --argjson v "$version" '{yaml: .yaml, expected_version: $v}' <<<"$workflow_payload")
		api_daemon POST /api/workflow-library/dogfood/revisions "$revision_payload" >/dev/null ||
			die "dogfood draft created but executable save failed — inspect it in Workflows"
		log "workflow: installed executable 'dogfood'"
	else
		api_daemon GET /api/workflow-library/dogfood >/dev/null 2>&1 ||
			die "could not install dogfood workflow"
		log "workflow: 'dogfood' created concurrently (operator version preserved)"
	fi
fi


# ---- 10. smoke ------------------------------------------------------------

rc=0
smoke_ok()   { printf '  ok    %s\n' "$1"; }
smoke_fail() { printf '  FAIL  %s\n' "$1" >&2; rc=1; }
if curl -q -fsS --noproxy '*' --max-time 2 http://127.0.0.1:4100/healthz >/dev/null 2>&1; then
	smoke_ok "forge broker /healthz (local readiness only, not GitHub authentication)"
else
	smoke_fail "forge broker unavailable — restart $BROKER_WRAPPER"
fi
environments=$(api_daemon GET /api/environments) || environments='[]'
environment=$(jq -c '.[] | select(.name == "dogfood")' <<<"$environments")
[ -n "$environment" ] || environment='{}'
state=$(jq -r '.status.state // "missing"' <<<"$environment")
if [ "$state" = ready ]; then
	smoke_ok "environment dogfood: ready"
else
	smoke_fail "environment dogfood: $state — fix blockers and Re-check in Settings"
	jq -r '.status.blockers[]? | "  blocker: " + .message' <<<"$environment" >&2
fi
workflow=$(api_daemon GET /api/workflow-library/dogfood) || workflow='{}'
revision=$(jq -r '.entry.current_revision // empty' <<<"$workflow")
if jq -e '.entry.available == true and .entry.descriptor.actions == [] and .entry.descriptor.reviews == false' \
	<<<"$workflow" >/dev/null && [ -n "$revision" ] &&
	api_daemon GET "/api/workflows/revisions/$revision" | jq -e '.definition.environment == "dogfood"' >/dev/null; then
	smoke_ok "workflow dogfood: executable, requires dogfood, no publish authority"
else
	smoke_fail "workflow dogfood unavailable, publishes, or no longer requires dogfood — inspect the operator library in Workflows"
fi
if api_daemon GET /api/projects/ompire | jq -e --arg c "$ROOT" \
	'.checkout_path == $c and .fetch_remote == "qa-self" and .setup_state == "ready"' >/dev/null; then
	smoke_ok "project ompire: ready with local qa-self fetch"
else
	smoke_fail "project ompire missing or not ready with local qa-self fetch"
fi


login=$(curl -fsS -K "$QA_DIR/.curlrc" https://api.github.com/user | jq -r '.login // empty' 2>/dev/null)
[ "$login" = "$BOT_LOGIN" ] && smoke_ok "agent PAT authenticates as @$BOT_LOGIN" ||
	smoke_fail "agent PAT returned '$login' (expected $BOT_LOGIN)"

# The daemon derives this from the same launch environment that later runs
# gh pr create. Compare only safe fields to the bot's immutable setup record.
daemon_gh=$(api_daemon GET /api/gh 2>/dev/null || true)
daemon_gh_state=$(jq -r '.identity.state // empty' <<<"$daemon_gh" 2>/dev/null)
daemon_gh_login=$(jq -r '.identity.login // empty' <<<"$daemon_gh" 2>/dev/null)
daemon_gh_host=$(jq -r '.identity.host // empty' <<<"$daemon_gh" 2>/dev/null)
daemon_gh_source=$(jq -r '.identity.credential_source // empty' <<<"$daemon_gh" 2>/dev/null)
if [ "$daemon_gh_state" = ready ] && [ "$daemon_gh_login" = "$BOT_LOGIN" ] && [ "$daemon_gh_host" = github.com ]; then
	smoke_ok "daemon GitHub identity: @$daemon_gh_login on $daemon_gh_host (${daemon_gh_source:-unknown source})"
else
	smoke_fail "daemon GitHub identity: state=${daemon_gh_state:-no response}, login=${daemon_gh_login:--}, host=${daemon_gh_host:--}; expected @$BOT_LOGIN on github.com"
fi

if git ls-remote "$SSH_URL" HEAD >/dev/null 2>&1; then
	smoke_ok "ssh ls-remote $REPO"
else
	smoke_fail "ssh ls-remote $REPO — is the bot's ssh key still on the account?"
fi

# The daemon's own verdict, verbatim — this is what the ship gate consumes.
gpg_json=$(api_daemon GET /api/gpg 2>/dev/null || true)
gpg_state=$(jq -r '.state // empty' <<<"$gpg_json" 2>/dev/null)
gpg_key=$(jq -r '.selected.fingerprint // empty' <<<"$gpg_json" 2>/dev/null)
case "$gpg_state" in
ready)
	smoke_ok "daemon GPG probe: ready as ${gpg_key:-?} (ship gate allows Sign & commit)" ;;
locked)
	smoke_fail "daemon GPG probe: locked — warm gpg-agent (see env.sh comment) and restart the daemon" ;;
agent_unavailable)
	smoke_fail "daemon GPG probe: agent_unavailable — start it with 'gpg-connect-agent /bye' and re-check" ;;
no_key)
	smoke_fail "daemon GPG probe: no_key — gpg_signing_key in $OMPIRE_CONFIG should be $BOT_FPR" ;;
ambiguous)
	smoke_fail "daemon GPG probe: ambiguous — several usable keys; select $BOT_FPR in Settings" ;;
missing)
	smoke_fail "daemon GPG probe: missing — gpg is not on the daemon's PATH" ;;
*)
	smoke_fail "daemon GPG probe: ${gpg_state:-no response} — expected ready"
	;;
esac

if api_daemon GET /api/projects | jq -e --arg n "$NAME" '.[] | select(.name == $n)' >/dev/null; then
	smoke_ok "daemon serves project '$NAME'"
else
	smoke_fail "project '$NAME' not visible via daemon API"
fi

# A task-scoped daemon result is intentionally created only when a real task
# enters Ship flow. Do not spawn a model task merely for setup smoke; verify
# the same registered sandbox target through the configured host CLI here.
registered_upstream=$(api_daemon GET "/api/projects/$NAME" 2>/dev/null | jq -r '.upstream_url // empty' 2>/dev/null || true)
if [ "$registered_upstream" = "$SSH_URL" ]; then
	smoke_ok "registered GitHub target: github.com/$REPO"
else
	smoke_fail "registered target is '$registered_upstream' (expected '$SSH_URL')"
fi
repo_preflight=$(gh api --hostname github.com "repos/$REPO" 2>/dev/null || true)
if jq -e '
	.archived == false and .disabled == false and .has_issues == true and
	(.pull_request_creation_policy == "all" or
	 (.pull_request_creation_policy == "collaborators_only" and
	  (.role_name | if type == "string" then (length > 0 and ascii_downcase != "none") else false end) and
	  .permissions.pull == true))
' <<<"$repo_preflight" >/dev/null 2>&1 &&
	gh api --hostname github.com "repos/$REPO/pulls?per_page=1" 2>/dev/null | jq -e 'type == "array"' >/dev/null 2>&1; then
	smoke_ok "sandbox target eligible for GitHub preflight: github.com/$REPO"
else
	smoke_fail "sandbox target is not eligible for GitHub preflight: github.com/$REPO"
fi

# Do the bot's commits show "Verified" on GitHub?
if git -C "$CHECKOUT" rev-parse HEAD >/dev/null 2>&1; then
	vr=$(curl -fsS -K "$QA_DIR/.curlrc" "https://api.github.com/repos/$REPO/commits?per_page=1" 2>/dev/null |
		jq -r '.[0].commit.verification | "\(.verified) \(.reason)"' 2>/dev/null)
	case "$vr" in
	"true "*) smoke_ok "bot commits verify on GitHub" ;;
	"false bad_email")
		smoke_fail "commits show Unverified (bad_email) — the noreply address is inactive:
          enable 'Keep my email addresses private' at https://github.com/settings/emails
          (bot account), then push another commit to re-check" ;;
	*) warn "commit verification state: ${vr:-unknown}" ;;
	esac
fi

if curl -fsS "$DAEMON_URL/" -H "Authorization: Bearer $DAEMON_TOKEN" 2>/dev/null | grep -q 'No frontend build found'; then
	smoke_fail "daemon serves the placeholder page — frontend/dist missing?"
else
	smoke_ok "UI served at $DAEMON_URL"
fi

# Task agents get LLM auth through the pi-auth-gateway tunnel (workshop.yaml):
# it must be reachable on localhost:4000 (port-forwarded here if needed).
gw_code=$(curl -q -sS -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:4000/ 2>/dev/null) || gw_code=000
if [ -n "$gw_code" ] && [ "$gw_code" != 000 ]; then
	smoke_ok "auth gateway reachable on :4000 (HTTP $gw_code)"
else
	warn "auth gateway NOT reachable on :4000 — task agents will have no LLM auth."
	warn "forward it, e.g.: ssh -N -R 4000:localhost:4000 <this-host> from the gateway side"
	smoke_fail "auth gateway :4000 unreachable"
fi

	cat <<-EOF

	==> QA environment setup complete
	    daemon:   $DAEMON_URL  (pid $(cat "$PIDFILE"), log $OMPIRE_DATA/daemon.log)
	    open UI:  $DAEMON_URL/ (bootstrap from $OMPIRE_DATA/token on the trusted operator browser only)
	    broker:   localhost:4100 (pid $(cat "$BROKER_PIDFILE"), log $BROKER_LOG)
	    restart:  $BROKER_WRAPPER (foreground; setup manages its background pid)
	    project:  $NAME -> $REPO (checkout $CHECKOUT, base $BASE_BRANCH)
	    dogfood:  project ompire, workflow dogfood; grant environment access in Spawn
	    restart:  $DAEMON_WRAPPER
	    identity: . $QA_DIR/env.sh   (any shell that should act as @$BOT_LOGIN)
	$([ $rc -eq 0 ] || printf '\n    NOTE: one or more smoke checks failed — see above.\n')
	EOF

exit "$rc"
