# Troubleshoot the daemon

## The daemon will not start

Ompire refuses to start on a bad configuration file rather than ignoring what
it cannot parse. The error names the problem:

```
ompire-daemon: unknown config key(s) in /home/you/.config/ompire/config.toml: gpg_key
```

Check the key against [Configuration](../reference/configuration.md). A
misspelled key is an error, not a silently ignored line.

Malformed TOML and wrong value types fail the same way, naming the key and
what it received.

## The UI loads but shows nothing

The frontend needs the bearer token. If `localStorage` was cleared or you are
on a different browser, re-open with the token in the query string:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

If the token was rotated, every open WebSocket is closed with code `1008` and
every client must re-authenticate with the new token.

## The UI is empty after a snap upgrade

Your state is not stored in the snap revision. It lives in
`~/snap/ompire/common`, and the daemon reports the directory it actually
opened:

```sh
curl -H "Authorization: Bearer $(cat ~/snap/ompire/common/token)" \
  http://127.0.0.1:4173/api/daemon/info
```

A `data_dir` ending in a revision, such as `~/snap/ompire/x8`, means the common
directory was not available and the daemon fell back to the revision
directory.

Upgrading from a snap old enough to have stored state per revision moves it
once, on the first start of the new revision, and logs it:

```
carried operator state (token, ompire.db-wal, ompire.db) from /home/you/snap/ompire/x8 to /home/you/snap/ompire/common
```

No such line means there was nothing to carry. That is normal on every start
after the first. Nothing is ever deleted from the revision directory, so look
there before concluding anything is lost:

```sh
ls ~/snap/ompire/*/db/ompire.db
```

A carry-forward that cannot finish stops the daemon instead of starting it on
an empty database, and names both directories:

```
ompire-daemon: could not carry operator state from /home/you/snap/ompire/x8 to /home/you/snap/ompire/common: ...
```

Two things suppress the carry-forward deliberately: a `data_dir` set in
[`config.toml`](../reference/configuration.md), because a directory you named
is yours and another install's state does not get written into it; and a data
directory that already holds a database, because a carry-forward never
overwrites.

Finally, an empty **Tasks** list is not the same as empty state — archived
tasks are hidden from it. Check **Projects** before concluding that an upgrade
lost anything.

## The daemon chip shows disconnected

The WebSocket dropped. The frontend reconnects on its own and receives a fresh
authoritative snapshot when it does — no state is lost by a reconnect, and
running work is unaffected. If it stays disconnected, the daemon process is
gone; check its logs.

## A task failed during spawn

The failing step and its stderr are attached to the task. The steps fail for
characteristic reasons:

| Step | Common cause |
|---|---|
| `fetch` | The project's checkout has gone missing, or its [fetch remote](../reference/projects.md#fetch-remote) is unreachable |
| `clone` | No write access to `task_dir_root`, or the target path exists |
| `branch` | `origin/<base_branch>` does not exist — check the base branch the task was accepted with, on its detail view |
| `inputs` | A [handoff input](../reference/task-spawn.md#handoff-inputs) could not be installed: its destination is occupied, an ancestor is not an ordinary directory, the retained bytes no longer match what was accepted, or the base branch moved after the launch was accepted. The message names the path or identity. Nothing ran, and the clone is left for you to inspect |
| `workshop` | Container tooling unavailable, or the launch exceeded its timeout |

A `workshop` failure after a successful launch usually means the container
started but did not register; the lock file was missing or unreadable.

## A project will not register

Adopting a checkout validates it, so the refusal names the problem: the path
does not exist, is not the top level of a git work tree, has no remote with
the name you gave, or has no commits yet. Fix it in your own repository —
Ompire never edits a checkout it did not create — and submit again.

A URL refusal means the form is not one of `https://`, `ssh://`, or
`git@host:owner/repo`. Local paths and `git://` are deliberately not accepted.

## A project is stuck on "cloning"

It is not: the card resolves either way. If the daemon was restarted mid-clone,
the next startup marks the project `failed` with "interrupted by daemon
restart" and you can retry from the card. A clone is never resumed
automatically.

A failed clone shows git's own stderr. The usual causes are an unreachable or
private repository — the clone uses your git configuration with prompts
disabled, so anything needing a password fails immediately rather than
hanging — or no write access to the checkout root.

## Spawn refuses with "project is not ready"

The project's checkout setup has not finished, or it failed. Open the Projects
view and either wait for the clone or retry it. See
[Projects](../reference/projects.md#setup-state).

## Shipping is refused

Usually the GPG key. `GET /api/gpg` reports the current state, and anything
other than `ready` blocks a commit. The state names which problem it is —
`locked` (cold passphrase cache), `ambiguous` (several keys, none selected),
`no_key`, `missing` (GnuPG not installed), `agent_unavailable`, or `error` —
and each has a different fix. See [Configure GPG
signing](configure-gpg-signing.md) for the table.

The GitHub CLI identity is the other common cause; it is refused separately and
names the account and repository.

The other refusals are a ship already in flight for that task, an unsupported
mode, or unmet `retain` preconditions. All are reported with a reason, and all
are refused before any Git operation runs.

## A session looks stuck

A session that has been silent past the stall threshold — 300 seconds by
default — is reported as `stalled` and raised to the `notify` tier. That is a
heuristic, not a fact: a long-running build looks identical to a wedged agent.

Check the session's live output before intervening. You can steer it, send a
follow-up, or interrupt it from the task detail view.

Raise `stall_threshold` if your work legitimately involves long silences.

## No desktop notifications

Ompire uses `notify-send`. It degrades in stages and logs which stage it hit:

- `notify-send` not on `PATH` — notifications disabled, badge count still works.
- No reachable D-Bus session bus — same.
- `notify-send` without `--action` support — notifications appear without the
  Open button.

The badge count and the tab title are the reliable signal in all cases. Set
`notifications_enabled = false` to turn desktop notifications off deliberately.

## The daemon was killed mid-task

On restart, Ompire re-establishes what it can. Sessions being recovered start
as `starting` and settle into `idle` or `failed` once the resumed agent is
ready or fails. Recovery fan-out is bounded — four concurrent resumes by
default — because each one is a real container-side agent startup.

An interrupted review leaves the task clone untouched — the reviewer reads an
isolated copy — so a crash mid-review is nothing to clean up.

An interrupted delivery is reconciled at startup against what actually happened,
and startup never signs, pushes, or creates a pull request on your behalf. If it
cannot establish an effect's outcome, the task says so explicitly: Ship flow
shows the action, its expected target, the observed evidence, and the recheck,
adopt, retry, and abandon decisions. Nothing dependent runs and cleanup is
refused until you resolve it. See
[Ship flow](../reference/ship-flow.md#interrupted-effects).

A clone parked by an older Ompire still carries `refs/ompire/review-orig` or
`refs/ompire/ship-orig`. Startup restores those and removes the marker only when
the restoration verifies. A ref that is still present after a restart means the
clone could not be restored safely: the marker is kept as evidence, and that one
task is blocked — review, drafting, delivery, and cleanup all refuse and name the
surviving ref. Resolve the clone by hand, then restart.

Session status itself does not survive a restart — it is in-memory state that
is rebuilt, not replayed.

## Finding the details

```sh
curl -sS http://127.0.0.1:4173/api/daemon/info \
  -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)"
```

Returns the version, bind address, port, config path, data directory, and the
audit log path when one exists.

## A project says its launch configuration needs a decision

This appears after upgrading from a release that had templates, when the
upgrade found something it would not decide for you: two templates that
disagreed about a field, a template that pinned a model, or a retired
`judge_model` still set in your `config.toml`. It is separate from checkout setup — a
perfectly healthy checkout can still be blocked here.

Open the project's card. The panel lists every distinct old value with the
template it came from, and nothing is pre-selected: pick the values you want,
choose a [model profile](../reference/model-profiles.md) or explicitly choose
no default, tick any acknowledgement it asks for, and save. Launching works
again immediately, and the old values stay recorded afterwards in case you
want to look at them.

Nothing else is affected while you decide. Other projects launch normally, and
tasks already running are untouched.

## A task says it needs a configuration before it can continue

Two different gaps produce this, and the panel asks only for what is actually
missing. In both cases the task's workspace, branch, sessions, run history,
review history and pull-request facts are intact; reading, stopping, and
cleaning it up were never blocked.

**The launch inputs were never recorded.** The task predates them. The model,
thinking level, preamble, and any spawn-time overrides it actually used were
never persisted, today's project and profile settings are not evidence of
those, and there is no fallback to `main`. Confirm a model profile, a base
branch, an additions source, and a preamble.

**The workflow definition was never recorded.** The task recorded a workflow
*name*, and a name is not a procedure: what those prompts and routes said at
the time is gone. Ompire offers the current definition of that task's own
workflow name as a candidate, with its revision and the definition itself
readable, and checks that it can account for the steps, kinds and sessions
already on record.

Tick the acknowledgements and confirm. That pins future behavior only — it
makes no claim about the turns already taken, recreates nothing, and changes no
recorded branch or session identity. The record keeps the boundary: attempts up
to the confirmation ran under a procedure nobody kept, and the panel says so.

Confirming also changes one thing about how the task behaves from here: when a
step leaves no valid result or a route cannot be decided, the run now stops and
waits for you instead of asking a model to classify it. The panel states this
before you confirm.

If the run was interrupted mid-flight, a **Continue** action appears afterwards.
Confirmation itself starts nothing. Review and shipping stay separate, explicit
actions. An archived task of this vintage needs no confirmation at all.

### It says the current definition cannot explain this task

The compatibility check found something on record the candidate cannot account
for — a step it does not declare, a step recorded as a different kind, a
session it does not declare, or a current position it has no step for. The
problems are listed.

Ompire will not remap steps or point the task at a different workflow: that
would relabel the run rather than continue it. The task stays exactly as it is
— readable, stoppable, and cleanable — and is not resumable. Inspect its
history and its sessions through the escape hatch, or clean it up.

## A run stopped and is waiting for me

There are two kinds of waiting, and the card says which.

A **gate** is the workflow asking you to look at something — the `bugfix`
escalation, for instance. **Resume** finishes it and the run continues at the
next step.

A **stopped** run is the engine refusing to guess: a step that had to leave a
result left none that could be read, or a route could not be decided from what
was recorded. The card names the step and the reason. **Retry** makes another
attempt at *that step*. It never continues past it, and it never edits what was
recorded.

A retried agent step is told its previous attempt left no valid result and that
files may already have changed, so it inspects the working tree rather than
redoing work. A retried decision re-reads exactly the same recorded evidence —
so if it did not decide before, it will not decide now, and it will stop again.
That is the honest answer; the way forward is to steer the session yourself
through the escape hatch, or to stop the task.

Retrying does not get you past a limit the workflow set. If the step has used
up the attempts its definition allows, the retry sends the run to that
workflow's gate instead of trying again.

Either action names the attempt you were looking at, so a stale browser tab or
a double click is refused rather than applied to something else. If you get a
conflict, reload and look again.

## A task says its workflow definition is unavailable

The task pinned a definition revision that cannot be read: the retained
document is missing, damaged, or written for a format this daemon does not
implement — the last one usually means a downgrade.

Only that task is blocked. It is not resumed, no prompt is sent, nothing is
published, and its position and history are untouched. Everything else runs
normally, and the task stays listed and readable so you can see what happened.
Ompire will not substitute today's definition of the same name, because running
a task under a document it never accepted is exactly the failure the pinning
prevents.

If you downgraded, upgrading again restores it. Otherwise the task can be
inspected and cleaned up, but not continued.

## The judge is not using the model I configured

There is no judge. The workflow engine no longer runs a model of its own, and
`judge_model` in `config.toml` is retired and configures nothing.

What used to happen: when a step left no readable result, or a route could not
be decided, a reserved LLM session was asked to classify it and the run
continued on the answer. That step was not part of the workflow you reviewed
and left no record of its own. Now the run stops and tells you what was missing
— see [A run stopped and is waiting for me](#a-run-stopped-and-is-waiting-for-me).

Your old `judge_model` value and any per-task judge binding are kept as
upgrade evidence so you can still see what was configured; neither configures
anything. Every model a run uses now belongs to a declared step you can see in
the Spawn preview. See
[Configuration](../reference/configuration.md#retired-keys) and [Spawn a
task](spawn-a-task.md#override-a-single-step).
