# The trust boundary

Ompire's security model rests on one line: the daemon is trusted, and
everything the agent touches is not. Most of the architecture follows from
where that line is drawn.

## Why there is a line at all

A coding agent is a probabilistic process reading untrusted input — issue
text, repository contents, tool output, its own earlier reasoning. Any of that
can steer it. The question is not whether an agent can be induced to do
something unintended, but what it can reach when it is.

So Ompire keeps the agent away from the things whose misuse cannot be undone:
your signing key, your forge credentials, your main checkout, and the decision
about whether its own work is acceptable.

## What is on each side

The daemon holds the credentials, the registry, process supervision, review,
and publishing. The agent gets a clone, a container, and a prompt.

| Capability | Who holds it |
|---|---|
| GPG signing key | Daemon, on the host |
| Forge credentials (`gh`) | Daemon, on the host |
| Your main checkout | Daemon; the agent gets a clone |
| Commit, push, PR creation | Daemon |
| Review verdict | Review tool, host side, plus you |
| Writing code | Agent |
| Drafting commit and PR text | Agent |
| Retained result bytes | Daemon, outside the workspace |

The agent may propose. It does not publish.

## Isolation

Each task gets a local clone of your checkout — a full clone with its own
`.git`, not a Git worktree. Worktrees were rejected deliberately: they share
writable Git metadata with the main repository, so an agent inside one can
affect state outside it, and they are not self-contained at a container mount
path.

The clone runs inside a task-specific container. Cleanup removes the container
before deleting the clone, and refuses any path outside the configured task
root.

## Review the agent cannot influence

Review runs against the *host* side of the clone, driven by the daemon. The
agent being reviewed does not run the reviewer and does not mediate the
verdict. An agent that could grade its own work would make the review
ceremonial.

To show the complete task delta rather than the last commit, the review
temporarily resets the clone. That temporary state is protected by a durable
Git ref recorded before the reset, so an interrupted review is restorable
rather than lost.

## Publishing

The agent drafts the commit message and pull-request text — that is genuinely
useful writing. The daemon does everything else: the signed commit, the push,
the pull-request creation, using host-side credentials the agent never sees.

Shipping fails closed. If the signing key is not ready to sign, the attempt is
refused before any Git operation runs, and the refusal names which condition it
was. An indeterminate key state is treated as unusable rather than attempted
and failed halfway.

The signing key, signature format, and signing program all come from the
operator's own configuration, never from the task clone. That clone is
writable by the agent, so trusting its Git configuration would let it choose
who signs — or, through `gpg.program`, which binary the daemon runs on the
host. The daemon then verifies that the commits it produced really carry the
intended key's signature before pushing anything.

## Keeping a result without publishing

Not every task ends in a commit. One that investigates a problem or drafts a
plan produces files worth keeping, and keeping them is a different decision from
publishing them.

Capture is a trusted daemon operation. It reads the task's workspace under the
daemon's privileges, not the agent's, and the agent has no say in what is
captured: the operator names the paths, and the retained bytes live in the
daemon's own owner-private store, outside the clone and unreachable from the
container. Cleanup can then delete the workspace without destroying the result.

What comes back out is still the agent's writing, and it is treated that way.
Retained text is shown as escaped source — Markdown is not rendered, HTML and
SVG are not executed, embedded resources are not fetched, and links are not
followed. Nothing downloaded is executable. A manifest and matching checksums
prove *identity*: they say these are the bytes you accepted, never that the
bytes are correct or that anything they declare is permitted.

Accepting a result is retention, not authority. It advances no workflow, answers
no review, and makes nothing publishable — which is why the Results panel is a
separate surface from Review and Ship rather than another approval on the same
one.

Capture refuses recognizable credential material rather than storing a redacted
copy, because a redacted file retained under a checksum you later trust is worse
than no file. That check is bounded and cannot recognize every secret, so a
retained result is still sensitive content a human has to read.

## Where the boundary is currently weaker than intended

Two gaps are worth knowing about, because documentation that only describes the
intended model would be misleading.

**Network access is ambient.** Policy-controlled network access per workflow
is direction, not current behavior. The container has the network access its
environment gives it.

**Secret detection in captured results is bounded.** It recognizes private-key
blocks, GitHub tokens, authorization-header values, credential-bearing URLs, and
the daemon's own token. It is a guard against the obvious mistake, not a
guarantee that a retained result contains no secret.

## Single operator, localhost

The daemon binds to `127.0.0.1` and authenticates with one bearer token
generated on first run and stored with owner-only permissions. There is no
user model, no roles, and no audit of who acted — because there is exactly
one operator.

This is a real boundary, not a placeholder: binding elsewhere exposes both the
API and the token to the network, and nothing else in the design compensates
for that.
