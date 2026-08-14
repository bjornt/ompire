#!/usr/bin/env bash
#
# Provision and rotate a least-privilege GitHub identity for an AI QA agent
# (dogfooding: clone over SSH, push branches, open PRs, GPG-signed commits).
#
# The identity lives in a dedicated GitHub *bot account* (created once by a
# human in the GitHub UI) so the agent never touches a personal account.
#
# Two trust planes, kept strictly separate:
#
#   MANAGEMENT plane — your `gh` CLI auth for the BOT account, with scopes
#     admin:public_key + admin:gpg_key. Only this script uses it, only when
#     you run it, to add/remove SSH+GPG keys on the bot account. The
#     credential is read out of gh on each run and is NEVER written into the
#     identity directory. One-time setup:
#         gh auth login --web --scopes "admin:public_key,admin:gpg_key"
#         # log into github.com as the BOT account in the browser
#     (or, if the bot is already a gh account: gh auth refresh
#         --user <bot> --scopes "admin:public_key,admin:gpg_key")
#
#   AGENT plane — a fine-grained PAT you create once in the GitHub UI and
#     hand to this script (interactively, via --token, or by writing the
#     token file yourself). GitHub has no API to mint PATs; the manual step
#     buys HARD per-repo restriction: repository selection confines the
#     token to exactly the QA repo(s) — it cannot see other repos, cannot
#     create repos, cannot flip visibility, and needs ZERO account
#     permissions (key management stays with this script). `setup` prints
#     the exact creation steps; the canonical storage location is:
#         $QA_AGENT_DIR/token     (mode 0600)
#
#     PAT settings:  Repository access:      only the QA repositories
#                    Repository permissions: Contents RW, Pull requests RW,
#                                            Issues RW, Actions RW,
#                                            Workflows RW (Metadata R implicit)
#                    Account permissions:    none
#     PATs expire (max ~1 year) — re-do the step with `rotate token`.
#
# --repo owner/name (repeatable) makes setup verify the agent token can
# actually access (and push to) those repos before any key is uploaded.
#
# Everything local lives under one directory:
#
#     $QA_AGENT_DIR/                     (default ~/.qa-agent, mode 0700)
#       token              agent-plane OAuth token / PAT          (0600)
#       .curlrc            curl config with the agent auth header (0600)
#       state.json         remote key ids + metadata for rotation (0600)
#       id_ed25519{,.pub}  SSH key, public half on the bot account
#       gnupg/             dedicated GPG home with the signing key (0700)
#       gpg-public.asc     armored public signing key (reference)
#       known_hosts        github.com host keys from api.github.com/meta
#       gitconfig          bot identity + commit.gpgsign
#       env.sh             source this to act as the bot (GH_TOKEN, ...)
#
# Commands:
#     setup                create whatever is missing (token, ssh key, gpg key)
#     rotate ssh|gpg|all   recreate key material: upload new via the management
#                          plane, then delete the old key from GitHub
#     rotate token         replace the agent PAT (e.g. before it expires)
#     status               verify both planes, remote keys vs local, ssh auth
#     teardown [--yes] [--delete-local]
#                          delete our SSH+GPG keys from the bot account; with
#                          --delete-local also wipe $QA_AGENT_DIR
#
# Notes:
#   - PATs cannot be revoked via API; delete them in the bot account at
#     https://github.com/settings/personal-access-tokens
#   - The GPG key is passphrase-protected with the passphrase stored at
#     $QA_AGENT_DIR/.gpg-passphrase (0600) — not for secrecy but because the
#     ompire daemon's ship gate only recognizes passphrase-*cached* keys.
#     Warm gpg-agent (done automatically on key rotation and by the QA
#     daemon wrapper) with:
#       gpg --pinentry-mode loopback --passphrase-file \
#         "$QA_AGENT_DIR/.gpg-passphrase" --clearsign <<<warm
#     Protect $QA_AGENT_DIR like a password; rotate often with `rotate all`.
#   - The management credential lives in ~/.config/gh. If the agent runs on
#     this machine, run it in a sandbox WITHOUT access to that config,
#     otherwise the plane separation is moot.
#   - If you point --dir inside a git repo, add it to .gitignore.
#
# Dependencies: curl, jq, gpg (2.1+), ssh-keygen, ssh, git, and gh (management
# plane only).

set -euo pipefail
umask 077

# ---- constants -----------------------------------------------------------

DIR="${QA_AGENT_DIR:-$HOME/.qa-agent}"
ADMIN_SCOPES="admin:public_key admin:gpg_key"
KEY_TITLE_PREFIX="qa-agent"
GH_API="https://api.github.com"
GH_META_URL="https://api.github.com/meta"

STATE=""          # set by ensure_dir/require_state
API_CODE=""
API_BODY=""
ADMIN_TOKEN=""    # set by resolve_admin; never persisted
ADMIN_CURLRC=""   # temp file, removed on exit
ADMIN_LOGIN=""
LOCKED=0

# ---- options -------------------------------------------------------------

OPT_TOKEN=""
OPT_NAME=""
OPT_EMAIL=""
OPT_YES=0
OPT_DELETE_LOCAL=0
OPT_BOT=""
OPT_REPOS=()

usage() {
	sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d' | sed '$d'
	printf '\nUsage: %s [options] <command> [args]\n\n' "${0##*/}"
	printf 'Commands: setup | rotate ssh|gpg|token|all | status | teardown | help\n\n'
	printf 'Options:\n'
	printf '  --dir PATH        identity directory (default %s)\n' "$DIR"
	printf '  --bot LOGIN       bot account to manage via gh (default: gh'\''s active account)\n'
	printf '  --repo OWNER/NAME verify the agent token can access this repo (repeatable)\n'
	printf '  --token TOK       agent fine-grained PAT; "-" reads stdin.\n'
	printf '                    Without it, setup prints PAT creation steps and prompts.\n'
	printf '                    You can also write the PAT to %s/token yourself (chmod 600)\n' "$DIR"
	printf '  --name NAME       git author name  (default: bot account login)\n'
	printf '  --email EMAIL     git author email (default: id+login@users.noreply.github.com)\n'
	printf '  --yes             skip teardown confirmation\n'
	printf '  --delete-local    teardown: also remove %s\n' "$DIR"
}

log()  { printf '==> %s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

cleanup() {
	[ -z "$ADMIN_CURLRC" ] || rm -f "$ADMIN_CURLRC"
	[ "$LOCKED" -eq 1 ] && rm -rf "$DIR/.lock"
}

# ---- state ---------------------------------------------------------------

ensure_dir() {
	mkdir -p "$DIR"
	chmod 700 "$DIR"
	DIR=$(cd "$DIR" && pwd)
	STATE="$DIR/state.json"
	[ -f "$STATE" ] || printf '{}' >"$STATE"
}

require_state() {
	[ -f "$DIR/state.json" ] && [ -s "$DIR/token" ] ||
		die "no identity in $DIR — run '$0 setup' first"
	DIR=$(cd "$DIR" && pwd)
	STATE="$DIR/state.json"
}

acquire_lock() {
	mkdir -p "$DIR"
	if ! mkdir "$DIR/.lock" 2>/dev/null; then
		die "another instance is running (lock dir: $DIR/.lock)"
	fi
	LOCKED=1
}

state_get() { # state_get <jq-path> -> value or empty
	jq -r "$1 // empty" "$STATE" 2>/dev/null || true
}

state_merge() { # state_merge <json-object> — shallow-merges into state
	local tmp
	tmp=$(mktemp "$DIR/.state.XXXXXX")
	jq -s '.[0] * .[1]' "$STATE" <(printf '%s' "$1") >"$tmp"
	mv "$tmp" "$STATE"
}

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---- github api ----------------------------------------------------------
# api CURLRC METHOD PATH [JSON] -> API_BODY + API_CODE; headers in $DIR/.headers

api() {
	local curlrc=$1 method=$2 path=$3 data=${4:-}
	local -a args=(-sS --max-time 30 -K "$curlrc" -X "$method"
		-D "$DIR/.headers" -H "Content-Type: application/json")
	[ -n "$data" ] && args+=(-d "$data")
	local out
	out=$(curl "${args[@]}" -w $'\n%{http_code}' "$GH_API$path") ||
		die "network error on $method $path"
	API_CODE=${out##*$'\n'}
	API_BODY=${out%$'\n'*}
}

api_agent() { api "$DIR/.curlrc" "$@"; }
api_admin() { api "$ADMIN_CURLRC" "$@"; }

api_delete_admin() { # 204 or already-gone 404 are both fine
	api_admin DELETE "$1"
	case "$API_CODE" in
	204) return 0 ;;
	404) warn "remote object already gone: $1"; return 0 ;;
	*) die "failed to delete $1 (HTTP $API_CODE): $API_BODY" ;;
	esac
}

write_curlrc() { # write_curlrc TOKEN PATH
	cat >"$2" <<-EOF
	header = "Authorization: Bearer $1"
	header = "Accept: application/vnd.github+json"
	header = "X-GitHub-Api-Version: 2022-11-28"
	EOF
	chmod 600 "$2"
}

# ---- management plane (your gh auth for the bot account) ------------------

resolve_admin() {
	command -v gh >/dev/null ||
		die "gh CLI is required for key management — https://cli.github.com"
	local -a who=(gh auth token)
	[ -n "$OPT_BOT" ] && who+=(--user "$OPT_BOT")
	ADMIN_TOKEN=$(env -u GH_TOKEN -u GITHUB_TOKEN "${who[@]}" 2>/dev/null) ||
		die "no gh token for ${OPT_BOT:+@$OPT_BOT }the active gh account — run:
       gh auth login --web --scopes \"admin:public_key,admin:gpg_key\"
       (log into github.com as the BOT account in the browser)"
	ADMIN_CURLRC=$(mktemp "${TMPDIR:-/tmp}/qa-agent-admin.XXXXXX")
	write_curlrc "$ADMIN_TOKEN" "$ADMIN_CURLRC"
	ADMIN_TOKEN="" # drop from shell memory; only the temp curlrc is used
	api_admin GET /user
	[ "$API_CODE" = 200 ] || die "gh token rejected by GitHub (HTTP $API_CODE): $API_BODY"
	ADMIN_LOGIN=$(jq -r '.login // empty' <<<"$API_BODY")
	[ -n "$ADMIN_LOGIN" ] || die "could not parse login from /user"
	[ -z "$OPT_BOT" ] || [ "$ADMIN_LOGIN" = "$OPT_BOT" ] ||
		die "gh token for --user $OPT_BOT resolves to @$ADMIN_LOGIN"
	check_scopes "$ADMIN_SCOPES" \
		"run: gh auth refresh ${OPT_BOT:+--user $OPT_BOT }--scopes \"admin:public_key,admin:gpg_key\"
       (fine-grained PAT as gh auth instead? it needs account permissions:
        'Git SSH keys' RW and 'GPG keys' RW)"
}

check_admin_match() { # management plane must operate on OUR bot account
	local want
	want=$(state_get '.login')
	[ -z "$want" ] || [ "$ADMIN_LOGIN" = "$want" ] ||
		die "gh manages keys for @${ADMIN_LOGIN} but this identity belongs to @${want}
       re-run with: --bot ${want}"
}

# ---- agent plane ----------------------------------------------------------

save_token() {
	printf '%s' "$1" >"$DIR/token"
	# Keep the token out of `ps`: curl reads the auth header from this file.
	write_curlrc "$1" "$DIR/.curlrc"
	chmod 600 "$DIR/token"
}

LOGIN=""
USER_ID=""

validate_agent_token() { # sets LOGIN, USER_ID; returns 1 on invalid token
	api_agent GET /user
	[ "$API_CODE" = 200 ] || return 1
	LOGIN=$(jq -r '.login // empty' <<<"$API_BODY")
	USER_ID=$(jq -r '.id // empty' <<<"$API_BODY")
	[ -n "$LOGIN" ] && [ -n "$USER_ID" ]
}

validate_new_token() { # validate_new_token TOKEN — validate BEFORE persisting
	local tmpc
	tmpc=$(mktemp "$DIR/.token-check.XXXXXX")
	write_curlrc "$1" "$tmpc"
	validate_token_curlrc "$tmpc" && rm -f "$tmpc" || { rm -f "$tmpc"; return 1; }
}

validate_token_curlrc() { # validate_token_curlrc CURLRC — sets LOGIN, USER_ID
	api "$1" GET /user
	[ "$API_CODE" = 200 ] || return 1
	LOGIN=$(jq -r '.login // empty' <<<"$API_BODY")
	USER_ID=$(jq -r '.id // empty' <<<"$API_BODY")
	[ -n "$LOGIN" ] && [ -n "$USER_ID" ]
}

pat_instructions() {
	local repos="your QA repo(s)"
	[ ${#OPT_REPOS[@]} -eq 0 ] || repos="${OPT_REPOS[*]}"
	cat >&2 <<-EOF

	  Create a fine-grained personal access token for the QA bot account:

	    1. log into github.com as @${ADMIN_LOGIN:-<bot-account>}
	    2. open https://github.com/settings/personal-access-tokens/new
	    3. token name:   qa-agent        (anything recognizable)
	    4. expiration:   your choice — max ~1 year; 'rotate token' re-does this step
	    5. repository access: "Only select repositories" → $repos
	    6. repository permissions:
	         Contents       Read and write    (push branches)
	         Pull requests  Read and write    (open/update PRs)
	         Issues         Read and write    (file QA findings)
	         Actions        Read and write    (read + trigger workflow runs)
	         Workflows      Read and write    (push .github/workflows changes)
	       (Metadata read-only is added automatically)
	    7. account permissions: none — key management stays with this script
	    8. generate and copy the token (github_pat_...)

	EOF
}

obtain_agent_token() { # -> stdout: the PAT; empty when none was provided
	local tok=""
	if [ -n "$OPT_TOKEN" ]; then
		if [ "$OPT_TOKEN" = - ]; then tok=$(cat); else tok=$OPT_TOKEN; fi
	elif [ -t 0 ] && [ -r /dev/tty ]; then
		pat_instructions
		printf '  Paste the fine-grained PAT: ' >/dev/tty
		read -r -s tok </dev/tty
		printf '\n' >/dev/tty
	fi
	printf '%s' "$tok"
}

check_scopes() { # check_scopes REQUIRED... ADVICE — against last api() headers
	local required=$1 advice=$2 hdr s missing normalized
	hdr=$(tr -d '\r' <"$DIR/.headers" | sed -n 's/^[Xx]-[Oo]auth-[Ss]copes:[[:space:]]*//p')
	if [ -z "$hdr" ]; then
		warn "token exposes no x-oauth-scopes header (fine-grained PAT?). Cannot introspect permissions."
		warn "$advice"
		return 0
	fi
	missing=""
	normalized=",$(printf '%s' "$hdr" | tr -d ' '),"
	for s in $required; do
		case "$normalized" in
		*",$s,"*) ;;
		*) missing="$missing $s" ;;
		esac
	done
	[ -z "$missing" ] || die "token is missing required scopes:$missing — $advice"
	log "scopes ok: $hdr"
}

verify_repos() { # verify_repos [OWNER/NAME ...] — prove the agent token reaches them
	local r push
	for r in "$@"; do
		api_agent GET "/repos/$r"
		case "$API_CODE" in
		200)
			push=$(jq -r '.permissions.push // empty' <<<"$API_BODY")
			[ "$push" = true ] || warn "no explicit push permission reported for $r"
			log "repo access ok: $r"
			;;
		404)
			die "agent token cannot see $r — add it to the PAT's repository selection at https://github.com/settings/personal-access-tokens" ;;
		*) die "repo check failed for $r (HTTP $API_CODE): $API_BODY" ;;
		esac
	done
}

git_name()  { printf '%s' "${OPT_NAME:-$(state_get '.login')}"; }
git_email() { printf '%s' "${OPT_EMAIL:-$(state_get '.user_id')+$(state_get '.login')@users.noreply.github.com}"; }

# ---- ssh key (managed via the management plane) ---------------------------

ssh_upload() { # ssh_upload TITLE PUBFILE -> remote key id
	local payload
	payload=$(jq -n --arg t "$1" --arg k "$(<"$2")" '{title: $t, key: $k}')
	api_admin POST /user/keys "$payload"
	[ "$API_CODE" = 201 ] ||
		die "failed to add SSH key (HTTP $API_CODE): $API_BODY
       management auth needs scope admin:public_key (or account permission 'Git SSH keys' RW)"
	jq -r '.id' <<<"$API_BODY"
}

rotate_ssh() {
	local title="$KEY_TITLE_PREFIX-ssh-$(date -u +%Y%m%d-%H%M%S)"
	local tmp old_id new_id fpr
	tmp=$(mktemp -d "$DIR/.rotate.XXXXXX")
	log "generating new ed25519 ssh key"
	ssh-keygen -q -t ed25519 -N '' -C "$title" -f "$tmp/id_ed25519"
	new_id=$(ssh_upload "$title" "$tmp/id_ed25519.pub")
	log "uploaded to GitHub as key id $new_id ($title)"
	old_id=$(state_get '.ssh.remote_id')
	if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
		api_delete_admin "/user/keys/$old_id"
		log "removed previous ssh key $old_id from GitHub"
	fi
	mv -f "$tmp/id_ed25519" "$tmp/id_ed25519.pub" "$DIR/"
	chmod 600 "$DIR/id_ed25519"
	rm -rf "$tmp"
	fpr=$(ssh-keygen -lf "$DIR/id_ed25519.pub" | awk '{print $2}')
	state_merge "$(jq -n --argjson id "$new_id" --arg t "$title" --arg f "$fpr" --arg now "$(now_utc)" \
		'{ssh: {remote_id: $id, title: $t, fingerprint: $f, created_at: $now}}')"
	log "ssh key active: $fpr"
}

# ---- gpg key (managed via the management plane) ---------------------------

gpg_upload() { # gpg_upload ARMORED -> remote key id
	local payload
	payload=$(jq -n --arg k "$1" '{armored_public_key: $k}')
	api_admin POST /user/gpg_keys "$payload"
	[ "$API_CODE" = 201 ] ||
		die "failed to add GPG key (HTTP $API_CODE): $API_BODY
       management auth needs scope admin:gpg_key (or account permission 'GPG keys' RW)"
	jq -r '.id' <<<"$API_BODY"
}

gpg_warm() { # cache the passphrase with gpg-agent so signing needs no pinentry
	local fpr
	fpr=$(state_get '.gpg.fingerprint')
	[ -n "$fpr" ] || return 0
	GNUPGHOME="$DIR/gnupg" gpg --batch --yes --pinentry-mode loopback \
		--passphrase-file "$DIR/.gpg-passphrase" --clearsign -u "$fpr" \
		<<<"warm" >/dev/null 2>&1 || warn "gpg-agent warm-up failed"
}

rotate_gpg() {
	local uid tmp old_id new_id fpr armored
	uid="$(git_name) <$(git_email)>"
	tmp=$(mktemp -d "$DIR/.rotate.XXXXXX")
	chmod 700 "$tmp"
	# Key shape: cert-only primary + signing subkey, passphrase-protected.
	# This mirrors a human operator's key so the ompire daemon's GPG gate
	# works unchanged: its probe looks for a signing *subkey* keygrip and
	# treats passphrase-*cached* keys as unlocked. An unprotected or
	# subkey-less key would read as locked/unknown forever and the ship
	# flow's "Sign & commit" would stay disabled. The passphrase lives in
	# $DIR/.gpg-passphrase (0600) — it is not secrecy, it is the gate's
	# unlock mechanism; gpg-agent is warmed with a ~1y cache TTL.
	local pass
	pass=$(head -c 24 /dev/urandom | base64)
	cat >"$tmp/gpg-agent.conf" <<-'EOF'
	default-cache-ttl 31536000
	max-cache-ttl 31536000
	EOF
	log "generating new ed25519 gpg key for '$uid' (cert primary + signing subkey)"
	GNUPGHOME="$tmp" gpg --batch --yes --pinentry-mode loopback --passphrase "$pass" \
		--quick-generate-key "$uid" ed25519 cert never >&2
	fpr=$(GNUPGHOME="$tmp" gpg --batch --with-colons --list-keys "$uid" |
		awk -F: '$1 == "fpr" {print $10; exit}')
	[ -n "$fpr" ] || die "gpg: could not determine fingerprint of new key"
	GNUPGHOME="$tmp" gpg --batch --yes --pinentry-mode loopback --passphrase "$pass" \
		--quick-add-key "$fpr" ed25519 sign never >&2
	armored=$(GNUPGHOME="$tmp" gpg --batch --armor --export "$fpr")
	new_id=$(gpg_upload "$armored")
	log "uploaded to GitHub as gpg key id $new_id (fingerprint $fpr)"
	old_id=$(state_get '.gpg.remote_id')
	if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
		api_delete_admin "/user/gpg_keys/$old_id"
		log "removed previous gpg key $old_id from GitHub"
	fi
	GNUPGHOME="$DIR/gnupg" gpgconf --kill gpg-agent >/dev/null 2>&1 || true
	rm -rf "$DIR/gnupg"
	mv "$tmp" "$DIR/gnupg"
	chmod 700 "$DIR/gnupg"
	printf '%s' "$pass" >"$DIR/.gpg-passphrase"
	chmod 600 "$DIR/.gpg-passphrase"
	GNUPGHOME="$DIR/gnupg" gpg --batch --armor --export "$fpr" >"$DIR/gpg-public.asc"
	chmod 644 "$DIR/gpg-public.asc"
	state_merge "$(jq -n --argjson id "$new_id" --arg f "$fpr" --arg now "$(now_utc)" \
		'{gpg: {remote_id: $id, fingerprint: $f, created_at: $now}}')"
	write_gitconfig
	gpg_warm
	log "gpg key active: $fpr (commit signing enabled; gpg-agent warmed)"
}

# ---- env files -----------------------------------------------------------

write_known_hosts() {
	local keys=""
	keys=$(curl -sS --max-time 20 "$GH_META_URL" |
		jq -r '.ssh_keys[]? | "github.com " + .' 2>/dev/null) || keys=""
	if [ -z "$keys" ]; then
		warn "could not fetch github.com host keys from $GH_META_URL; falling back to ssh-keyscan"
		keys=$(ssh-keyscan -t rsa,ecdsa,ed25519 github.com 2>/dev/null) ||
			die "ssh-keyscan github.com failed"
	fi
	printf '%s\n' "$keys" >"$DIR/known_hosts"
	chmod 644 "$DIR/known_hosts"
}

write_gitconfig() {
	local fpr
	fpr=$(state_get '.gpg.fingerprint')
	{
		printf '[user]\n\tname = %s\n\temail = %s\n' "$(git_name)" "$(git_email)"
		if [ -n "$fpr" ]; then
			printf '\tsigningkey = %s\n[commit]\n\tgpgsign = true\n' "$fpr"
		fi
	} >"$DIR/gitconfig"
	chmod 600 "$DIR/gitconfig"
}

write_env_sh() {
	# env.sh self-locates: the identity dir can be moved/copied (e.g. into a
	# QA container) without regenerating anything. GIT_SSH_COMMAND uses
	# explicit flags instead of an ssh_config file for the same reason —
	# ssh_config has no path expansion and would go stale on a copy.
	cat >"$DIR/env.sh" <<-'EOF'
	# Source this file (with bash) to act as the QA bot identity:
	#     . /path/to/.qa-agent/env.sh
	# The identity directory is relocatable; paths resolve from this file.
	# If git commit fails on a locked GPG key, warm gpg-agent once with:
	#     gpg --pinentry-mode loopback --passphrase-file \
	#       "$QA_AGENT_DIR/.gpg-passphrase" --clearsign <<<warm
	_qa_self="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
	export QA_AGENT_DIR="$_qa_self"
	export GH_TOKEN="$(cat "$QA_AGENT_DIR/token")"
	export GITHUB_TOKEN="$GH_TOKEN"
	export GNUPGHOME="$QA_AGENT_DIR/gnupg"
	export GIT_CONFIG_GLOBAL="$QA_AGENT_DIR/gitconfig"
	export GIT_SSH_COMMAND="ssh -i $QA_AGENT_DIR/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=$QA_AGENT_DIR/known_hosts -o StrictHostKeyChecking=yes"
	unset _qa_self
	EOF
	chmod 600 "$DIR/env.sh"
}

# ---- commands ------------------------------------------------------------

cmd_setup() {
	acquire_lock
	ensure_dir

	# 1. management plane (gh auth for the bot account, key-admin scopes)
	resolve_admin

	# 2. agent token (fine-grained PAT)
	if [ -s "$DIR/token" ]; then
		# the user may have written $DIR/token by hand — regenerate the curlrc
		[ -f "$DIR/.curlrc" ] || write_curlrc "$(cat "$DIR/token")" "$DIR/.curlrc"
		validate_agent_token || die "existing agent token rejected by GitHub (HTTP $API_CODE): $API_BODY
       replace $DIR/token with a valid fine-grained PAT and re-run setup"
		[ "$LOGIN" = "$ADMIN_LOGIN" ] ||
			die "agent token is for @$LOGIN but gh manages keys for @$ADMIN_LOGIN — re-run with --bot $LOGIN"
		state_merge "$(jq -n --arg l "$LOGIN" --argjson u "$USER_ID" '{login: $l, user_id: $u}')"
		log "agent token: reusing existing (login: $LOGIN)"
	else
		local tok
		tok=$(obtain_agent_token)
		if [ -z "$tok" ]; then
			pat_instructions
			die "no PAT provided — then either:
       save it to $DIR/token (chmod 600) and re-run, or
       pipe it:  <pat-command> | $0 --token - setup"
		fi
		validate_new_token "$tok" || die "PAT rejected by GitHub (HTTP $API_CODE): $API_BODY"
		[ "$LOGIN" = "$ADMIN_LOGIN" ] ||
			die "agent token is for @$LOGIN but gh manages keys for @$ADMIN_LOGIN — re-run with --bot $LOGIN"
		save_token "$tok"
		state_merge "$(jq -n --arg l "$LOGIN" --argjson u "$USER_ID" --arg now "$(now_utc)" \
			'{login: $l, user_id: $u, auth: {method: "pat", created_at: $now}}')"
		log "agent token: authorized as $LOGIN (id $USER_ID)"
	fi
	check_admin_match

	# 3. prove the agent token reaches the QA repos
	if [ ${#OPT_REPOS[@]} -eq 0 ]; then
		warn "no --repo given — cannot verify the PAT's repository selection"
	else
		verify_repos "${OPT_REPOS[@]}"
		local repos_json
		repos_json=$(jq -n --args '[$ARGS.positional[]]' -- "${OPT_REPOS[@]}")
		state_merge "$(jq -n --argjson r "$repos_json" '{repos: $r}')"
	fi

	# 4. ssh key (skip if already provisioned)
	if [ -n "$(state_get '.ssh.remote_id')" ]; then
		log "ssh key: already provisioned ($(state_get '.ssh.fingerprint')) — use 'rotate ssh' to replace"
	else
		rotate_ssh
	fi

	# 5. gpg key (skip if already provisioned)
	if [ -n "$(state_get '.gpg.remote_id')" ]; then
		log "gpg key: already provisioned ($(state_get '.gpg.fingerprint')) — use 'rotate gpg' to replace"
	else
		rotate_gpg
	fi

	# 6. env files
	write_known_hosts
	write_gitconfig
	write_env_sh

	cat >&2 <<-EOF

	==> QA identity ready for @$LOGIN
	    dir:      $DIR
	    ssh:      $(state_get '.ssh.fingerprint')
	    gpg:      $(state_get '.gpg.fingerprint')
	    activate: . $DIR/env.sh
	    verify:   $0 status
	EOF
}

cmd_rotate() {
	local what=${1:-all}
	acquire_lock
	require_state
	case "$what" in
	token)
		local tok
		tok=$(obtain_agent_token)
		if [ -z "$tok" ]; then
			pat_instructions
			die "no PAT provided — pipe it:  <pat-command> | $0 --token - rotate token"
		fi
		validate_new_token "$tok" || die "PAT rejected by GitHub (HTTP $API_CODE): $API_BODY — existing token left in place"
		[ "$LOGIN" = "$(state_get '.login')" ] ||
			die "new PAT is for @$LOGIN but this identity belongs to @$(state_get '.login') — existing token left in place"
		save_token "$tok"
		state_merge "$(jq -n --arg now "$(now_utc)" '{auth: {method: "pat", created_at: $now}}')"
		log "agent token replaced (login: $LOGIN)"
		log "delete the OLD PAT at https://github.com/settings/personal-access-tokens"
		# re-verify the recorded repos against the new PAT's selection
		local r
		while IFS= read -r r; do [ -n "$r" ] && verify_repos "$r"; done < <(state_get '.repos[]?')
		;;
	ssh | gpg | all)
		resolve_admin
		check_admin_match
		if [ "$what" = ssh ] || [ "$what" = all ]; then
			[ -f "$DIR/known_hosts" ] || write_known_hosts
			rotate_ssh
		fi
		if [ "$what" = gpg ] || [ "$what" = all ]; then
			rotate_gpg
		fi
		;;
	*) die "unknown rotate target '$what' (expected ssh|gpg|token|all)" ;;
	esac
	write_env_sh
	log "rotation complete — agents pick it up on next '. $DIR/env.sh'"
}

cmd_status() {
	require_state
	local rc=0
	if validate_agent_token; then
		printf 'agent:    %s (id %s)\n' "$LOGIN" "$USER_ID"
		local hdr
		hdr=$(tr -d '\r' <"$DIR/.headers" | sed -n 's/^[Xx]-[Oo]auth-[Ss]copes:[[:space:]]*//p')
		printf '          scopes: %s\n' "${hdr:-<not introspectable — fine-grained PAT, as expected>}"
		local r
		while IFS= read -r r; do
			[ -n "$r" ] || continue
			api_agent GET "/repos/$r"
			if [ "$API_CODE" = 200 ]; then
				printf 'repo:     %s reachable\n' "$r"
			else
				printf 'repo:     %s NOT REACHABLE (HTTP %s) — fix the PAT repository selection\n' "$r" "$API_CODE"
				rc=1
			fi
		done < <(state_get '.repos[]?')
	else
		printf 'agent:    TOKEN INVALID (HTTP %s) — %s\n' "$API_CODE" "$(jq -r '.message // empty' <<<"$API_BODY" 2>/dev/null)"
		rc=1
	fi
	if command -v gh >/dev/null; then
		resolve_admin
		printf 'mgmt:     gh as @%s (key administration)\n' "$ADMIN_LOGIN"
	else
		printf 'mgmt:     gh not installed — needed only for rotate/teardown\n'
	fi

	# ssh: local vs state vs remote
	local ssh_id ssh_fpr local_fpr=""
	ssh_id=$(state_get '.ssh.remote_id')
	ssh_fpr=$(state_get '.ssh.fingerprint')
	if [ -f "$DIR/id_ed25519.pub" ]; then
		local_fpr=$(ssh-keygen -lf "$DIR/id_ed25519.pub" | awk '{print $2}')
	fi
	printf 'ssh:      state=%s local=%s\n' "${ssh_fpr:-<none>}" "${local_fpr:-<missing>}"
	if [ -n "$ssh_id" ] && [ -n "$ADMIN_CURLRC" ]; then
		api_admin GET /user/keys
		if [ "$API_CODE" != 200 ]; then
			printf '          could not list remote ssh keys (HTTP %s)\n' "$API_CODE"
			rc=1
		elif jq -e --argjson id "$ssh_id" '.[] | select(.id == $id)' <<<"$API_BODY" >/dev/null; then
			printf '          remote id %s present on GitHub\n' "$ssh_id"
		else
			printf '          REMOTE KEY %s MISSING on GitHub — run: rotate ssh\n' "$ssh_id"
			rc=1
		fi
	fi
	[ -n "$ssh_fpr" ] && [ "$ssh_fpr" = "$local_fpr" ] || {
		printf '          state/local MISMATCH — run: rotate ssh\n'
		rc=1
	}

	# gpg: local vs state vs remote
	local gpg_id gpg_fpr local_gfpr=""
	gpg_id=$(state_get '.gpg.remote_id')
	gpg_fpr=$(state_get '.gpg.fingerprint')
	if [ -d "$DIR/gnupg" ]; then
		local_gfpr=$(GNUPGHOME="$DIR/gnupg" gpg --batch --with-colons --list-keys 2>/dev/null |
			awk -F: '$1 == "fpr" {print $10; exit}')
	fi
	printf 'gpg:      state=%s\n          local=%s\n' "${gpg_fpr:-<none>}" "${local_gfpr:-<missing>}"
	if [ -n "$gpg_id" ] && [ -n "$ADMIN_CURLRC" ]; then
		api_admin GET /user/gpg_keys
		if [ "$API_CODE" != 200 ]; then
			printf '          could not list remote gpg keys (HTTP %s)\n' "$API_CODE"
			rc=1
		elif jq -e --argjson id "$gpg_id" '.[] | select(.id == $id)' <<<"$API_BODY" >/dev/null; then
			printf '          remote id %s present on GitHub\n' "$gpg_id"
		else
			printf '          REMOTE KEY %s MISSING on GitHub — run: rotate gpg\n' "$gpg_id"
			rc=1
		fi
	fi
	[ -n "$gpg_fpr" ] && [ "$gpg_fpr" = "$local_gfpr" ] || {
		printf '          state/local MISMATCH — run: rotate gpg\n'
		rc=1
	}

	# end-to-end ssh auth as the bot
	if [ -f "$DIR/id_ed25519" ] && [ -f "$DIR/known_hosts" ]; then
		local out
		out=$(ssh -i "$DIR/id_ed25519" -o IdentitiesOnly=yes \
			-o UserKnownHostsFile="$DIR/known_hosts" -o StrictHostKeyChecking=yes \
			-o BatchMode=yes -o ConnectTimeout=10 -T git@github.com 2>&1) || true
		case "$out" in
		*"Hi ${LOGIN:-$(state_get '.login')}"*) printf 'ssh auth: ok (%s)\n' "$(grep -o 'Hi [^!]*!' <<<"$out" | head -n1)" ;;
		*)
			printf 'ssh auth: FAILED — %s\n' "$(head -n1 <<<"$out")"
			rc=1
			;;
		esac
	fi
	return "$rc"
}

cmd_teardown() {
	acquire_lock
	require_state
	resolve_admin
	check_admin_match
	if [ "$OPT_YES" -ne 1 ]; then
		printf 'This deletes the qa-agent SSH and GPG keys from @%s. Continue? [y/N] ' "$ADMIN_LOGIN" >&2
		local ans
		read -r ans
		case "$ans" in y | Y | yes) ;; *) die "aborted" ;; esac
	fi

	local ssh_id gpg_id
	ssh_id=$(state_get '.ssh.remote_id')
	gpg_id=$(state_get '.gpg.remote_id')
	[ -z "$ssh_id" ] || { api_delete_admin "/user/keys/$ssh_id"; log "removed ssh key $ssh_id"; }
	[ -z "$gpg_id" ] || { api_delete_admin "/user/gpg_keys/$gpg_id"; log "removed gpg key $gpg_id"; }

	# report any leftover keys we likely created (same title prefix)
	api_admin GET /user/keys
	if [ "$API_CODE" = 200 ]; then
		local leftovers
		leftovers=$(jq -r --arg p "$KEY_TITLE_PREFIX" '.[] | select(.title | startswith($p)) | "  \(.id) \(.title)"' <<<"$API_BODY")
		[ -z "$leftovers" ] || warn "other $KEY_TITLE_PREFIX-* ssh keys remain on the account:\n$leftovers"
	fi
	state_merge '{"ssh": null, "gpg": null}'

	cat >&2 <<-'EOF'

	==> Remote keys removed. Agent tokens cannot be revoked via API —
	    OAuth device token:  https://github.com/settings/applications
	    fine-grained PAT:    https://github.com/settings/tokens?type=beta
	EOF
	if [ "$OPT_DELETE_LOCAL" -eq 1 ]; then
		case "$DIR" in
		/ | "$HOME") die "refusing to delete unsafe dir: $DIR" ;;
		esac
		GNUPGHOME="$DIR/gnupg" gpgconf --kill gpg-agent >/dev/null 2>&1 || true
		rm -f "$ADMIN_CURLRC"
		ADMIN_CURLRC=""
		rm -rf "$DIR/.lock"
		LOCKED=0
		rm -rf "$DIR"
		log "deleted local identity dir $DIR"
	else
		log "local dir kept: $DIR (use --delete-local to remove)"
	fi
}

# ---- main ----------------------------------------------------------------

main() {
	local cmd=""
	local target=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--dir) DIR=$2; shift 2 ;;
		--bot) OPT_BOT=$2; shift 2 ;;
		--repo) OPT_REPOS+=("$2"); shift 2 ;;
		--token) OPT_TOKEN=$2; shift 2 ;;
		--name) OPT_NAME=$2; shift 2 ;;
		--email) OPT_EMAIL=$2; shift 2 ;;
		--yes) OPT_YES=1; shift ;;
		--delete-local) OPT_DELETE_LOCAL=1; shift ;;
		setup | rotate | status | teardown)
			[ -z "$cmd" ] || die "multiple commands given"
			cmd=$1
			shift
			# rotate takes a positional target (ssh|gpg|token|all); consume it
			# here so the option parser never trips over it.
			if [ "$cmd" = rotate ] && [ $# -gt 0 ]; then
				case "$1" in
				ssh | gpg | token | all) target=$1; shift ;;
				*) die "unknown rotate target '$1' (expected ssh|gpg|token|all)" ;;
				esac
			fi
			;;
		help | --help | -h) usage; exit 0 ;;
		*) die "unknown argument: $1 (try 'help')" ;;
		esac
	done
	[ -n "$cmd" ] || { usage; exit 2; }

	for dep in curl jq gpg ssh-keygen ssh git; do
		command -v "$dep" >/dev/null || die "missing dependency: $dep"
	done

	trap cleanup EXIT
	case "$cmd" in
	setup) cmd_setup ;;
	rotate) cmd_rotate "${target:-all}" ;;
	status) cmd_status ;;
	teardown) cmd_teardown ;;
	esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	main "$@"
fi
