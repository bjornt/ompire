# ADR 0027: Hand off model policy between turns and record what applied

- Status: Accepted
- Date: 2026-09-06

## Context

[ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md) pinned
one model policy per task: four role bindings, each step's declared role, and
the judge's role, resolved once at acceptance. Every agent step of both
built-in workflows declares `default`, so in practice a task had *one* active
pair, and the code said so — `ModelPolicy.for_step` read the same role map for
every consumer, and the workflow engine reused a cached child only when its
policy compared equal, refusing anything else outright.

That was honest for one policy per task and is not enough for a choice per
step. Three separate things break as soon as two consumers of the same task
can differ:

- **A session is shared.** In `bugfix`, `reproduce` and `validate-agent` both
  run in the `reproducer` session, and they exist to share a conversation. If
  they may run under different policies, "reuse the cached handle only when
  the policy matches" degrades to "refuse the second step", and starting a
  fresh child instead would throw away the context the named session exists to
  keep ([ADR-0008](0008-model-tasks-as-workflows-over-named-sessions.md)).
- **The native contract is asymmetric.** omp v18.1.10's RPC dispatcher offers
  `set_model` and `set_thinking_level` and no setter for the auxiliary roles:
  `--smol`, `--slow`, and `--plan` are start-time flags. Half of a policy can
  be changed in place; the other half cannot.
- **Restart recovery had nothing to restore.** `recovery.py` built one
  `default`-active policy for every session of a task. With one policy per
  task that was merely redundant; with a policy per step it is wrong, and the
  registry held no record of what any session had actually been running.
  A step-start record is not evidence either: a configuration can fail after
  the step opened, and the run would then resume under a policy that never
  applied.

The question this record answers is where a per-consumer policy is decided,
how it reaches a live native process without splitting the session, and what a
restart is entitled to believe about a session it did not start.

## Decision

**A task pins one complete binding per model consumer.** The version-2
execution-inputs document replaces the single role map with a `ConsumerBinding`
for every declared agent step and every engine-reserved auxiliary consumer —
today just the judge. Each binding carries its source profile name, separate
attribution for the profile and the role (an operator can override one and
inherit the other), the effective role, and the whole four-role snapshot that
profile bound. Runtime lookup is exact and **fails closed**: a consumer with no
stored binding is an error, never a fall back to a task-wide default, because
"the task's model" stopped being a single fact. The task-wide profile decision
is retained as what unoverridden consumers inherited, not as an executable
fallback.

**The judge is an ordinary consumer.** It keeps `slow` as its declared role and
gains a binding, attribution, and an override like any agent step. It has no
separate model setting and no hidden exception.

**A session records the policy it last verifiably ran.** `task_sessions` gains
a nullable applied-policy document: the complete `ModelPolicy`, its source
profile and role, the consumer that applied it, and whether it was verified or
derived by an upgrade. This is mutable execution state, deliberately a
different kind of fact from the task's immutable inputs — the task says what
each consumer *may* run, the session says what actually took effect and
therefore what a resume, a follow-up, review feedback, or ship drafting
continues with. Only accepted task bindings may supply it.

**The supervisor owns a per-session handoff boundary.** Every mutation of a
session's process, and every read that precedes a prompt, runs inside that
session's own lock — per session, not per task, so one wedged container cannot
stall its siblings, and never held across a turn, because a turn can wait for
an operator's answer. Inside it:

1. The live handle is re-read; one fetched before the wait is not trusted.
2. A policy change is refused unless the child is at a turn boundary —
   streaming, compaction, queued messages, and an unanswered question all
   refuse through the ordinary infrastructure-failure path. Configuration
   never aborts work in flight.
3. When at most the active pair differs, the process is kept and the pair is
   asserted over the acknowledged `set_model` / `set_thinking_level` controls,
   then read back and compared exactly. This runs even when the policy is
   *unchanged*: a cached handle is not evidence, and an operator `/model`
   inside the container would otherwise masquerade as the accepted policy.
4. When any auxiliary pair differs, the native session id is captured off the
   running child, the child is stopped gracefully so container-side omp flushes
   its session file, and a replacement is started with `--resume` and all four
   new pairs. The resumed identity is compared against the captured one: omp
   opens a new session when the recorded id names nothing, and that would look
   like continuity while being a fresh context. A fresh start is never
   substituted for a resume.
5. Any refusal, timeout, partial reconfiguration, identity mismatch, or failed
   applied-state write leaves **no prompt-capable unverified handle**. The
   candidate is stopped, the consumer fails, and the previous durable record
   and the conversation stand.
6. The verified policy is committed durably *before* the turn that depends on
   it, and authoritative model state is published only after success.

**A replacement inherits the logical session, not a new identity.** The
`(task_id, session_name)` key, the tracker entry, and the bounded replay
history carry over; the retired child is marked so its exit is not read as a
crash and cannot unregister or fail the replacement. Model choice is not part
of session identity. The per-session event channel closes with a distinct
non-terminal code so a connected browser reconnects to the replacement and
replays the carried-over transcript instead of ending it.

**Recovery restores what applied, or says it cannot.** Each session resumes on
its own applied record. Where none exists, a step interrupted *before* its
prompt went out supplies its accepted binding — the same decision the run is
about to make anyway — and anything else leaves the session unresumed with the
reason stated. Guessing from the first step that shares the session, or from
today's profiles, is not available.

**The upgrade re-expresses, and does not invent.** Migration 0014 converts a
version-1 document using only its own stored roles, step roles, and judge role;
it does not re-read the source profile or consult the current workflow, and a
step added since acceptance was never accepted for that task. Every consumer's
binding is attributed to the workflow, because version 1 had no way to express
a per-step choice and labelling these as operator overrides would fabricate
one. Resumable sessions of those tasks get a continuation policy from the same
pinned map — the stored judge role for the `judge` session, `default` for the
rest — labelled `migrated`. It says what the session continues under and makes
no claim about turns already taken. (A version-1 daemon in fact resumed every
session on `default`, judge included; this record fixes the future without
rewriting the past.) Tasks with no pinned inputs keep ADR-0026's explicit
reconciliation requirement.

This record extends ADR-0026 rather than replacing it: the resolution still
happens once, at acceptance, under the same write reservation, against the same
fingerprint — the fingerprint now covers every consumer's *complete* role map,
so an edit touching only a profile's `slow` pair invalidates a review even
though no summary line changed.
[ADR-0007](0007-use-native-omp-rpc.md)'s native RPC boundary, ADR-0008's named
sessions and sequential task ownership,
[ADR-0018](0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md)'s
built-in trust boundary, and
[ADR-0025](0025-store-global-model-profiles-separately-from-launch-policy.md)'s
profile ownership are unchanged. No credential, auth profile, or provider
configuration enters a model policy.

## Consequences

An operator can put one step on a different profile or a different role without
touching the workflow, the profiles, or any other step, and the reviewed
resolution is what executes. Two steps sharing a session can run genuinely
different providers, models, thinking levels, and auxiliary maps while keeping
one conversation.

The cost is that some transitions restart a process. Changing only the active
pair is cheap; changing an auxiliary pair is a graceful stop, a session-file
flush, a resume, and a fresh ready handshake — tens of seconds against a real
container, and a window in which the session reads as `starting` and the event
channel reconnects. That is the price of a native contract with start-time role
flags, and it is paid only when the operator actually asked for a different
auxiliary map.

Denormalization grows. The full four-role snapshot is now stored per consumer
rather than once per task, so a twelve-row workflow stores the same profile
several times. That is deliberate: it is what lets a task keep running exactly
as accepted after its source profiles are edited or deleted, and resolution
reads each distinct profile once and shares the immutable pairs in memory.

A restart is now honest about what it does not know. A session with no applied
record is not resumed, which means an installation whose rows predate this
change may see a session respawn fresh rather than continue — with the reason
reported — instead of silently resuming under a policy nobody chose.

Failures are louder in one specific way: a step whose session is mid-turn when
its policy must change fails rather than waiting or interrupting. In the
engine this cannot normally happen — steps run sequentially and reconfigure at
turn boundaries — so it surfaces a genuine race, such as an operator prompt
landing between a step's idle and its successor's handoff.

Revisit this if omp gains RPC setters for the auxiliary roles (the replacement
path then collapses into the in-place one), if a consumer needs its policy
changed mid-turn, or if workflows become versioned artifacts whose exact
version is pinned per task — which would let recovery reconstruct a step's
binding rather than requiring the session to have recorded it.

## Alternatives considered

### Keep one policy per task and make steps choose a role only

Restricting a row override to the role, with one profile for the whole task,
would have avoided process replacement entirely: all four flags would stay
constant and only `set_model`/`set_thinking_level` would ever be needed. It was
rejected because it cannot express the case the epic exists for — a step that
should run on a different provider — and because it would make "which profile"
a task-level question forever, so mixing a thorough provider for the fix step
with a cheap one for validation would require two tasks.

### Start a fresh native session when the auxiliary map changes

Skipping `--resume` and simply spawning a new child would make the transition
faster and remove the identity check. It was rejected because it silently
destroys the conversation, which is exactly what a named session is for: two
steps sharing `reproducer` would stop sharing anything, and the operator would
see a transcript that looks continuous over a context that is not.

### Derive the resume policy from the task instead of recording it

Recovery could pick the binding of whichever step last has a record for a
session, avoiding the new column. It was rejected because a step record says a
step *started*, not that its configuration succeeded — the failure this ADR
spends a commit ordering to make unambiguous. A crash between a failed handoff
and its retry would then resume on a policy the process never ran.

### Reconfigure lazily, at the first prompt after a change

The handoff could be deferred until a turn is actually sent, folding the
boundary into `prompt`. It was rejected because it puts a process restart
inside the step that is trying to prompt, where a failure is much harder to
attribute, and because it removes the one place — an idle boundary the engine
already owns — where a policy change is unambiguously safe.
