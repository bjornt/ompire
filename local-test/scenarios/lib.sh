#!/usr/bin/env bash
#
# local-test/scenarios/lib.sh — LOCAL-TESTING.PLAN.md Part 9: the shared
# runbook harness. Sourced by every scenario script (never executed
# directly); it gives each runbook the same driving and assertion contract:
#
#   state root    $LOCAL_TEST_STATE if set, else <repo>/local-test/.state
#                 (identical to every sibling tool)
#   daemon        port from <state>/home/.config/ompire/config.toml, else
#                 $LOCAL_TEST_PORT, else 4173; bearer token from
#                 <state>/home/.local/share/ompire/token   (the ompctl/
#                 gpgctl resolution, reused verbatim)
#   driving       `api METHOD PATH [JSON]` — REST with auth; sets API_CODE,
#                 prints the body
#                 `wait_for DESC SECONDS CMD…` — 1 s poll, loud last state
#   asserting     `ok DESC` / `fail DESC [DETAIL]` counting checks; the
#                 EXIT trap prints a summary and exits non-zero on any FAIL
#   slugs         `unique_slug <flow>` — per-run task slugs so re-runs
#                 never collide with leftover tasks
#   git/forge     `forge_git ARGS…` — git under the state root's gitconfig
#                 + GNUPGHOME (insteadOf rewrites + verify key), for
#                 assertions against the bare repo
#   settings      `setting_set KEY VALUE` — PUT /api/settings with an EXIT
#                 trap restoring the previous value
#   ws            `ws_start [MATCH…]` / `ws_stop` / `ws_grep` / `ws_count`
#                 — record /api/ws via scenarios/ws-watch
#
# Idle-waiting convention: after `spawn_task`, a runbook waits on the
# observable it actually needs next — `workflow_complete` (the work step
# finished, i.e. the primary session went idle) or `review_open` (whose
# 200 *is* the desired next action). There is no passive session-status
# probe: the daemon publishes session status only over WS.
#
# Requires: curl, jq, python3, uv (ws-watch runs under the daemon venv).

# --- state root / daemon resolution -----------------------------------------

LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$LIB_DIR/../.." && pwd)
STATE_ROOT=${LOCAL_TEST_STATE:-$REPO_ROOT/local-test/.state}
HOME_DIR=$STATE_ROOT/home
CONFIG=$HOME_DIR/.config/ompire/config.toml
TOKEN_FILE=$HOME_DIR/.local/share/ompire/token
DAEMON_PIDFILE=$HOME_DIR/.local/share/ompire/daemon.pid
DAEMON_WRAPPER=$STATE_ROOT/daemon.sh
GITCONFIG=$STATE_ROOT/gitconfig
GNUPGHOME_DIR=$STATE_ROOT/gnupg
WS_BIN=$LIB_DIR/ws-watch

DAEMON_PORT=${LOCAL_TEST_PORT:-4173}
if [ -f "$CONFIG" ] && grep -qE '^port[[:space:]]*=' "$CONFIG"; then
	DAEMON_PORT=$(sed -n 's/^port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$CONFIG" | tail -1)
fi
DAEMON_URL=http://127.0.0.1:$DAEMON_PORT

CTL=$REPO_ROOT/local-test
GHCTL=$CTL/ghctl
WSCTL=$CTL/wsctl
OMPCTL=$CTL/ompctl
GPGCTL=$CTL/gpgctl
REVIEW=$CTL/review
FORGE=$CTL/forge
ENV_TOOL=$CTL/env

# The persistent local-test environment currently registers the built-in
# single-step workflow, whose primary session is `main`. `primary_session`
# still verifies that declaration through the public agent surface and can
# recover a non-main primary from the review gate's 409 detail.
PROJECT=sandbox

die() { printf 'error: %s\n' "$*" >&2; exit 2; }
command -v curl >/dev/null || die "curl is required"
command -v jq   >/dev/null || die "jq is required"

# --- assertions --------------------------------------------------------------

CHECKS_OK=0
CHECKS_FAIL=0
LAST_WAIT_STATE=""

# Assertions print to stderr: helper functions whose stdout is captured
# (spawn_task, wait_for conditions, …) must not pollute it.
ok()   { CHECKS_OK=$((CHECKS_OK + 1)); printf '  ok    %s\n' "$*" >&2; }
fail() {
	CHECKS_FAIL=$((CHECKS_FAIL + 1))
	printf '  FAIL  %s\n' "$*" >&2
	[ $# -gt 1 ] && printf '        %s\n' "$2" >&2
	return 0
}

# Assert a command's exit status (its output becomes the failure detail).
assert() { # assert DESC CMD ARGS…
	local desc=$1; shift
	local tmp; tmp=$(mktemp)
	if "$@" >"$tmp" 2>&1; then
		ok "$desc"
	else
		fail "$desc" "$(head -5 "$tmp")"
	fi
	rm -f "$tmp"
}

_api_token() {
	[ -f "$TOKEN_FILE" ] || die "no daemon token at $TOKEN_FILE — run 'local-test/env up' first"
	cat "$TOKEN_FILE"
}

# One engine, two faces: _api_run performs the request in *this* shell so
# API_CODE/API_BODY survive command substitution; api()/api_json() print.
_api_run() {
	local method=$1 path=$2 data=${3:-}
	local -a args=(-sS -X "$method" -H "Authorization: Bearer $(_api_token)")
	[ -n "$data" ] && args+=(-H "Content-Type: application/json" -d "$data")
	local out
	out=$(curl "${args[@]}" -w '\n%{http_code}' "$DAEMON_URL$path") ||
		die "request failed: $method $path (is the daemon up on $DAEMON_URL?)"
	API_CODE=${out##*$'\n'}
	API_BODY=${out%$'\n'*}
}

api() { # api METHOD PATH [JSON_BODY] — prints body, sets API_CODE
	_api_run "$@"
	printf '%s' "$API_BODY"
}

api_json() { # api_json METHOD PATH [JSON_BODY] — prints body, dies on non-JSON
	_api_run "$@"
	if ! jq -e . >/dev/null 2>&1 <<<"$API_BODY"; then
		die "$2 request returned non-JSON (code $API_CODE): $(head -c 300 <<<"$API_BODY")"
	fi
	printf '%s' "$API_BODY"
}

api_code() { # api_code METHOD PATH [DATA] — echo just the status code
	api "$@" >/dev/null
	printf '%s' "$API_CODE"
}

task_get()   { api_json GET "/api/tasks/$1"; }
task_field() { task_get "$1" | jq -r --arg f "$2" '.[$f] // empty'; }

primary_session() { # primary_session TASK_ID
	local task_id=$1 detail session
	_api_run GET "/api/tasks/$task_id/sessions/main/agent/state"
	case "$API_CODE" in
	200 | 409)
		printf 'main'
		return 0
		;;
	esac

	# A busy/non-live primary makes review return the declared session name.
	# This fallback is intentionally attempted only when `main` is not a
	# declared session, so it cannot open a review for the normal workflow.
	_api_run POST "/api/tasks/$task_id/review"
	detail=$(jq -r '.detail // empty' <<<"$API_BODY")
	session=$(sed -n "s/.*session '\([^']*\)'.*/\1/p" <<<"$detail")
	[ "$API_CODE" = 409 ] && [ -n "$session" ] ||
		die "cannot resolve primary session for task $task_id: $API_CODE $API_BODY"
	printf '%s' "$session"
}

# Spawn a task; echoes the task id. Deliberately does not wait for idle —
# see the header's idle-waiting convention.
spawn_task() { # spawn_task SLUG PROMPT
	local slug=$1 prompt=$2 body id
	body=$(jq -n --arg t "$PROJECT" --arg s "$slug" --arg p "$prompt" \
		'{template_name: $t, slug: $s, prompt: $p}')
	_api_run POST /api/tasks "$body"
	body=$API_BODY
	[ "$API_CODE" = 202 ] || die "spawn failed ($API_CODE): $body"
	id=$(jq -r .id <<<"$body")
	ok "task $id spawned (slug $slug)"
	echo "$id"
}

# --- polling -------------------------------------------------------------------

# Poll until a condition holds; report the last observed state on timeout.
wait_for() { # wait_for DESC SECONDS CMD ARGS…
	local desc=$1 secs=$2 rc
	shift 2
	local deadline=$((SECONDS + secs))
	while :; do
		LAST_WAIT_STATE=$("$@" 2>&1) && {
			printf '%s' "$LAST_WAIT_STATE"
			return 0
		}
		rc=$?
		if [ "$rc" -gt 1 ]; then
			fail "$desc" "failed: ${LAST_WAIT_STATE:-(no detail)}"
			return 1
		fi
		if [ $SECONDS -ge $deadline ]; then
			fail "$desc" "timed out after ${secs}s; last state: ${LAST_WAIT_STATE:-(none)}"
			return 1
		fi
		sleep 1
	done
}

# Condition helpers (used inside wait_for CMD…)
workflow_complete() { # workflow_complete TASK_ID — the work step finished
	# (= the primary session having gone idle once, single-step workflow).
	local status
	status=$(task_field "$1" workflow_status)
	if [ "$status" = complete ]; then
		printf 'complete'
		return 0
	fi
	printf 'workflow_status=%s' "${status:-none}"
	return 1
}

task_not_failed() { # task_not_failed TASK_ID
	local state
	state=$(task_field "$1" state)
	if [ "$state" = failed ]; then
		printf 'state=failed: %s' "$(task_field "$1" error)"
		return 1
	fi
	printf 'state=%s' "$state"
}

# Wait until the review gate accepts (its 200 is the desired next action)
# and the review is open; echoes the review state JSON (with `url`).
review_open() { # review_open TASK_ID
	_api_run POST "/api/tasks/$1/review"
	if [ "$API_CODE" = 200 ]; then
		printf '%s' "$API_BODY"
		return 0
	fi
	printf 'review start: %s %s' "$API_CODE" "$(jq -r '.detail // empty' <<<"$API_BODY" | head -c 200)"
	return 1
}

# --- uniqueness -----------------------------------------------------------------

unique_slug() { # unique_slug FLOW — e.g. happy-0822-1435-12345
	printf '%s-%s-%s' "$1" "$(date +%m%d-%H%M%S)" $$
}

# --- git against the forge ----------------------------------------------------

_SCRATCH_DIRS=()
forge_git() { # forge_git ARGS… — git with the state root's config + keyring
	GIT_CONFIG_GLOBAL=$GITCONFIG GNUPGHOME=$GNUPGHOME_DIR git "$@"
}
scratch_clone() { # scratch_clone BARE_REPO OUTPUT_VAR
	local bare=$1 output_var=$2 dir
	dir=$(mktemp -d /tmp/ompire-forge-XXXXXX)
	forge_git clone --quiet "$bare" "$dir"
	_SCRATCH_DIRS+=("$dir")
	printf -v "$output_var" '%s' "$dir"
}
_scratch_cleanup() {
	local dir
	for dir in "${_SCRATCH_DIRS[@]}"; do rm -rf "$dir"; done
	return 0
}

# --- daemon settings (restored on exit) --------------------------------------

_SETTING_RESTORES=()
setting_set() { # setting_set KEY JSON_VALUE — PUT with exact-layer restore
	local key=$1 requested=$2 body previous provenance value_json
	value_json=$(jq -ce . <<<"$requested") ||
		value_json=$(jq -cn --arg value "$requested" '$value')
	_api_run GET /api/settings
	[ "$API_CODE" = 200 ] || die "cannot read setting $key ($API_CODE): $API_BODY"
	body=$API_BODY
	previous=$(jq -c --arg key "$key" '.settings[$key]' <<<"$body")
	provenance=$(jq -r --arg key "$key" '.provenance[$key]' <<<"$body")
	_api_run PUT /api/settings "$(jq -cn --arg key "$key" --argjson value "$value_json" '{$key: $value}')"
	[ "$API_CODE" = 200 ] || die "setting $key=$requested rejected ($API_CODE): $API_BODY"
	_SETTING_RESTORES+=("$key"$'\t'"$provenance"$'\t'"$previous")
	ok "setting $key -> $requested (was: $previous from $provenance)"
}
_setting_restore_all() {
	local i entry key provenance previous
	for ((i = ${#_SETTING_RESTORES[@]} - 1; i >= 0; i--)); do
		entry=${_SETTING_RESTORES[$i]}
		IFS=$'\t' read -r key provenance previous <<<"$entry"
		if [ "$provenance" = override ]; then
			_api_run PUT /api/settings "$(jq -cn --arg key "$key" --argjson value "$previous" '{$key: $value}')"
		else
			_api_run DELETE "/api/settings/$key"
		fi
		[ "$API_CODE" = 200 ] || {
			printf 'error: failed to restore setting %s: %s %s\n' "$key" "$API_CODE" "$API_BODY" >&2
			return 1
		}
	done
	return 0
}

# --- websocket recording --------------------------------------------------------------

WS_OUT=""
WS_PID=0
_ws_snapshot_seen() {
	[ -s "$WS_OUT" ] && [ "$(jq -r 'select(.type == "snapshot") | .type' "$WS_OUT" 2>/dev/null | sed -n '1p')" = snapshot ]
}
ws_start() { # ws_start [TYPE…] — record /api/ws; exit once all TYPEs seen
	local -a match=()
	local m
	ws_stop
	for m in "$@"; do match+=(--match "$m"); done
	WS_OUT=$(mktemp /tmp/ws-watch-XXXXXX.jsonl)
	( uv run --project "$REPO_ROOT/daemon" --quiet python "$WS_BIN" \
		--url "ws://127.0.0.1:$DAEMON_PORT/api/ws?token=$(_api_token)" \
		--out "$WS_OUT" "${match[@]}" ) >/dev/null 2>&1 &
	WS_PID=$!
	wait_for "WebSocket snapshot recorded" 10 _ws_snapshot_seen >/dev/null
}
ws_stop() {
	if [ "$WS_PID" != 0 ]; then
		kill "$WS_PID" 2>/dev/null || true
		wait "$WS_PID" 2>/dev/null || true
	fi
	WS_PID=0
	return 0
}
ws_grep() { # ws_grep FIXED-STRING — matching recorded lines (if any)
	[ -n "$WS_OUT" ] && grep -F -- "$1" "$WS_OUT"
}
ws_count() { # ws_count JQ-FILTER — how many recorded envelopes match
	local n=0
	if [ -n "$WS_OUT" ] && [ -f "$WS_OUT" ]; then
		n=$(jq -c "select($1)" "$WS_OUT" 2>/dev/null | grep -c .) || n=0
	fi
	printf '%s' "$n"
}


# Condition for wait_for: true once at least one recorded envelope
# matches the jq filter (optionally scoped to a task id). Evaluated on
# every poll — a `test "$(ws_count ...)"` argument would be substituted
# once, freezing the count.
_ws_seen() { # _ws_seen JQ-FILTER [TASK_ID]
	local filter=$1
	if [ $# -gt 1 ]; then
		filter="$filter and (.payload.task_id == $2 or .payload.id == $2)"
	fi
	[ "$(ws_count "$filter")" -ge 1 ]
}
 
# Ship outcome: succeeds on shipped and returns 2 on a terminal error so
# wait_for stops immediately instead of turning a known failure into a timeout.
ship_done() { # ship_done TASK_ID
	local err
	err=$(jq -c "select(.type == \"ship_step\" and .payload.status == \"failed\" and .payload.task_id == $1)" "$WS_OUT" 2>/dev/null) || true
	if [ -n "$err" ]; then
		printf 'ship failed: %s' "$err"
		return 2
	fi
	if _ws_seen '.type == "ship_finished" and .payload.status == "shipped"' "$1"; then
		printf 'shipped'
		return 0
	fi
	printf 'committing/pushing'
	return 1
}

# One agent-authored commit in the clone (the `commit` scenario), waited
# for as the clone's commit count growing to WANT.
agent_commit() { # agent_commit TASK_ID CLONE WANT [PROMPT-SUFFIX]
	follow_up "$1" "[[scenario:commit]]
${4:-Agent change: stage and commit.}"
	wait_for "agent commit lands in the clone ($3 total)" 60 _clone_commits "$2" "$3"
}
_clone_commits() { # _clone_commits CLONE WANT
	local n
	n=$(git -C "$1" rev-list --count origin/main..HEAD 2>/dev/null) || { printf 'no origin/main'; return 1; }
	[ "$n" = "$2" ] && printf '%s' "$n" || { printf 'commits=%s want=%s' "$n" "$2"; return 1; }
}

# --- composite flow steps (shared by several runbooks) -------------------------


# The daemon returns as soon as it spawns llmvet; the port binds a moment
# later. Poll the review driver until llmvet answers.
review_ready() { # review_ready URL
	$REVIEW diff --url "$1" >/dev/null 2>&1
}

# Wait for the review gate, approve via the real llmvet UI.
approve_review() { # approve_review TASK_ID
	local review url
	review=$(wait_for "review gate accepts (session idle)" 60 review_open "$1")
	url=$(jq -r .url <<<"$review")
	[ -n "$url" ] && [ "$url" != null ] || die "review response carried no url: $review"
	wait_for "llmvet answers on $url" 30 review_ready "$url"
	$REVIEW approve --url "$url"
	# llmvet exits asynchronously; the daemon's reset-dance restore must
	# conclude before anything touches the clone again (draft/ship).
	wait_for "review concluded approved" 60 \
		_ws_seen '.type == "review_finished" and .payload.status == "approved"' "$1"
	ok "review approved via llmvet ($url)"
}

ship_squash() { # ship_squash TASK_ID
	local tid=$1 message pr_title pr_body
	api_json POST "/api/tasks/$tid/ship/draft" >/tmp/ship-draft-$tid.json
	[ "$API_CODE" = 200 ] || die "ship draft failed ($API_CODE): $(cat /tmp/ship-draft-$tid.json)"
	jq -e '.status == "drafted" and .draft.commit_message and .draft.pr_title' /tmp/ship-draft-$tid.json >/dev/null ||
		fail "draft parsed for task $tid" "$(cat /tmp/ship-draft-$tid.json)"
	message=$(jq -r '.draft.commit_message' /tmp/ship-draft-$tid.json)
	pr_title=$(jq -r '.draft.pr_title' /tmp/ship-draft-$tid.json)
	pr_body=$(jq -r '.draft.pr_body // ""' /tmp/ship-draft-$tid.json)
	$GPGCTL warm
	api_json POST "/api/tasks/$tid/ship/commit" \
		"$(jq -n --arg m "$message" --arg t "$pr_title" --arg b "$pr_body" \
			'{message: $m, pr_title: $t, pr_body: $b, mode: "squash"}')" >/dev/null
	[ "$API_CODE" = 200 ] || die "ship commit rejected for task $tid ($API_CODE)"
	wait_for "task $tid ship finishes" 120 ship_done "$tid"
	ok "task $tid shipped (pr: $(task_field "$tid" pr_url))"
}

follow_up() { # follow_up TASK_ID MESSAGE
	local session
	session=$(primary_session "$1")
	api POST "/api/tasks/$1/sessions/$session/agent/follow-up" \
		"$(jq -n --arg m "$2" '{message: $m}')" >/dev/null
	[ "$API_CODE" = 200 ] || die "follow-up rejected ($API_CODE)"
}

# Wait for a question_posted event for the task; echoes the question payload.
wait_question() { # wait_question TASK_ID
	wait_for "question posted for task $1" 90 _question_seen "$1"
}
_question_seen() {
	local line
	line=$(jq -c "select(.type == \"question_posted\" and .payload.task_id == $1)" "$WS_OUT" 2>/dev/null | tail -1) || true
	[ -n "$line" ] || { printf 'no question_posted yet'; return 1; }
	printf '%s' "$(jq -c .payload.question <<<"$line")"
}

# Scenario-specific cleanup callbacks are composed with the harness EXIT
# trap instead of replacing it. Callbacks must be function names.
_EXIT_CLEANUPS=()
register_cleanup() { _EXIT_CLEANUPS+=("$1"); }
_run_registered_cleanups() {
	local i
	for ((i = ${#_EXIT_CLEANUPS[@]} - 1; i >= 0; i--)); do
		"${_EXIT_CLEANUPS[$i]}" || return 1
	done
	return 0
}

# --- daemon lifecycle (crash-recovery / config-driven runbooks) ----------------

daemon_pid() { cat "$DAEMON_PIDFILE" 2>/dev/null; }

daemon_wait_ready() { # daemon_wait_ready SECONDS
	local deadline=$((SECONDS + ${1:-60})) token
	token=$(_api_token)
	while [ $SECONDS -lt $deadline ]; do
		curl -fsS -H "Authorization: Bearer $token" \
			"$DAEMON_URL/api/projects" >/dev/null 2>&1 && return 0
		sleep 0.5
	done
	return 1
}

daemon_kill9() { # daemon_kill9 — SIGKILL the daemon by pidfile
	local pid
	pid=$(daemon_pid)
	[ -n "$pid" ] || die "no daemon pid at $DAEMON_PIDFILE"
	kill -9 "$pid" 2>/dev/null || true
	for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.25; done
	fail "daemon (pid $pid) did not die after SIGKILL"
}

daemon_stop() { # daemon_stop — SIGTERM and wait
	local pid
	pid=$(daemon_pid)
	[ -n "$pid" ] || return 0
	kill "$pid" 2>/dev/null || return 0
	for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.25; done
	kill -9 "$pid" 2>/dev/null || true
}

daemon_start() { # daemon_start — (re)launch via the state root's wrapper
	nohup "$DAEMON_WRAPPER" >>"$HOME_DIR/.local/share/ompire/daemon.log" 2>&1 &
	echo $! >"$DAEMON_PIDFILE"
	wait_for "daemon ready again after restart" 120 daemon_wait_ready 120 ||
		die "daemon did not come back — see the daemon log"
	ok "daemon restarted (pid $(daemon_pid))"
}

# Set a config.toml key for the *next* daemon start (restart required); the
# previous value is restored on EXIT (with another restart only if needed).
config_set() { # config_set KEY VALUE
	local key=$1 value=$2 line prev=""
	line="^${key}[[:space:]]*="
	if [ -f "$CONFIG" ] && grep -qE "$line" "$CONFIG"; then
		prev=$(sed -n "s/^${key}[[:space:]]*=[[:space:]]*//p" "$CONFIG" | tail -1)
	fi
	if [ -z "$prev" ]; then
		printf '%s = %s\n' "$key" "$value" >>"$CONFIG"
	else
		sed -i "s|^${key}[[:space:]]*=.*|${key} = ${value}|" "$CONFIG"
	fi
	if [ "$prev" != "$value" ]; then
	_CONFIG_RESTORES+=("${key}=${prev}")
		ok "config $key -> $value (restart required; was: ${prev:-unset})"
	fi
}
_CONFIG_RESTORES=()
_config_restore_all() {
	local kv key prev changed=0
	for kv in "${_CONFIG_RESTORES[@]}"; do
		key=${kv%%=*} prev=${kv#*=}
		if [ -n "$prev" ]; then
			sed -i "s|^${key}[[:space:]]*=.*|${key} = ${prev}|" "$CONFIG"
		else
			sed -i "/^${key}[[:space:]]*=/d" "$CONFIG"
		fi
		changed=1
	done
	if [ $changed = 1 ]; then
		daemon_stop
		daemon_start
	fi
	return 0
}

_summary() {
	local rc=$? cleanup_rc=0
	trap - EXIT
	set +e
	ws_stop || cleanup_rc=1
	_run_registered_cleanups || cleanup_rc=1
	_setting_restore_all || cleanup_rc=1
	_config_restore_all || cleanup_rc=1
	_scratch_cleanup || cleanup_rc=1
	[ "$cleanup_rc" -eq 0 ] || rc=1
	if [ "$CHECKS_FAIL" -gt 0 ] || [ "$rc" -ne 0 ]; then
		printf 'FAILED: %d ok, %d FAIL\n' "$CHECKS_OK" "$CHECKS_FAIL" >&2
		exit 1
	fi
	printf 'passed: %d ok, 0 FAIL\n' "$CHECKS_OK"
	exit 0
}
trap _summary EXIT
