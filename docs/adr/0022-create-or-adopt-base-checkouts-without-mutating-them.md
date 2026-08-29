# ADR 0022: Create or adopt a project's base checkout without mutating operator repositories

- Status: Accepted
- Date: 2026-08-28

## Context

[ADR-0006](0006-give-every-task-a-separate-clone-and-workshop.md) establishes that every task works in its own disposable clone, created "from the registered local checkout". That decision is about the *task* clone. It says nothing about the checkout it clones from: who owns it, whether it must exist, whether Ompire may create one, or what Ompire may do to one it did not create.

Until this decision the answer was "nothing at all". Registering a project wrote a row. `checkout_path` was accepted verbatim or derived from a configured root, and neither form was checked. Spawn then assumed that path was a git work tree whose fetch remote was named `origin`. An operator whose checkout lived elsewhere, whose fork workflow named its upstream remote `upstream`, or who had not cloned the repository yet discovered all of it as a failed `fetch` step on their first task — after a task row, a branch name, and a clone path already existed. The registration form could not have told them, because it never looked.

Two capabilities are needed to fix that, and they pull in opposite directions. Validating an operator's existing checkout means running commands inside a repository Ompire does not own — the one repository the [vision](../VISION.md)'s security model says "remains untouched during ordinary task execution". Creating a checkout means writing a git repository to the host filesystem outside the daemon's task root, which is the first time Ompire has claimed that authority; ADR-0006's deletion confinement rule deliberately covers only the task root, so it does not bound this.

There is also a smaller but real correctness question underneath. The per-task clone's `origin` points at the base checkout, and branch creation, review, and ship merge-base logic all depend on that. "The remote to fetch in the base checkout" and "the remote the task clone was made from" had been the same string, `origin`, by coincidence rather than by design.

## Decision

A project records which of two modes produced its base checkout, and Ompire's authority over that checkout follows from the mode.

**Adoption is inspection, never repair.** When the operator supplies an existing checkout, Ompire accepts it only if it is an absolute path that is the top level of a non-bare git work tree, holds a remote with the project's fetch-remote name, and has at least one commit. Every command used to establish that is read-only git plumbing. Ompire does not add, rename, or repoint a remote, does not fetch, and does not touch the branch, index, working tree, or configuration — on the accepting path or on the refusing one. Detected remotes are offered to the operator as suggested upstream and fork values and are never applied without confirmation. Validation is synchronous, so registration answers ready-or-why rather than deferring the failure to the first spawn.

**Creation is bounded and never destructive.** In clone mode the destination is derived as `<effective checkout root>/<project name>` and is never supplied by the client. A pre-existing destination is refused before any work begins; Ompire never writes into, merges with, or overwrites a path it did not create. The clone is assembled at a staging path Ompire owns beside the destination and moved onto the destination with a single rename as its final step, so the destination either does not exist or holds a finished checkout. The staging tree is the only thing this process ever deletes. Accepted upstream and fork URLs are restricted to `https`, `ssh`, and `user@host:path` forms, because git's other transports read local paths or execute a helper command named by the URL, and option-shaped values are read by git as flags; both are refused before any subprocess is created. The clone runs with the operator's own git configuration, no injected credential, and every interactive prompt disabled, so an unreachable repository fails with git's message instead of hanging.

**A base checkout is never deleted by Ompire.** Unregistering a project removes the registry entry only. This holds equally for a checkout Ompire cloned: having created it does not make it Ompire's to destroy.

**Setup state is durable, and the filesystem is the authority for resolving it.** A project is `ready`, `cloning`, or `failed`, and that state — with the failing step's captured stderr — lives on the project row rather than only in progress events, so a client that reconnects mid-clone renders the truth from its snapshot. A daemon restart resolves every `cloning` row before the first snapshot is served, by inspecting the destination: a valid checkout makes the project ready, anything else makes it failed and removes the staging tree. A clone is never restarted automatically; retry is the operator's decision. A project that is not ready cannot be pointed at by a template or spawned against.

**The base checkout's fetch remote is a project fact; the task clone's `origin` is not.** Spawn fetches the base checkout using the project's configured fetch remote. The per-task clone's `origin` still points at that base checkout, and branch creation, review, and ship continue to use it unchanged.

The invariant is that Ompire may read an operator's repository and may create one in a bounded location it derives, but may never modify a repository it did not create, never overwrite a path it did not create, and never delete a base checkout at all.

## Consequences

Registration now means something. An operator learns that a path is wrong, that the remote they named is not there, or that the repository has no commits at the moment they are looking at the form and can fix it — instead of at the first `fetch` step of a task that has already been created. The common case of not having cloned the repository yet stops being a prerequisite the product cannot help with.

The read-only guarantee for adoption becomes a stated, testable property rather than an accident of not having implemented anything. It is worth the constraint it imposes: Ompire can never "fix" a checkout for the operator — not add the missing remote, not fetch it, not create the missing branch. Every such problem is reported and left for the operator to resolve in their own repository. That is the correct trade, because the alternative is a control plane that silently edits the repository the operator does most of their real work in.

Staging plus rename costs one extra directory and one extra step, and buys the property that makes everything else simple: there is no partial checkout for a later registration, a retry, or a startup reconciliation to misread. Retry needs no cleanup logic, and reconciliation is a single inspection.

The URL allowlist will refuse things git would accept. That is deliberate and it will occasionally be inconvenient — a local-path clone source, a `git://` mirror, and a plaintext `http://` remote are all rejected. The reason is narrow and worth restating: `git clone 'ext::sh -c …'` is host command execution under the operator's account, and it reaches git as an ordinary-looking string in a web form. An allowlist is the only form of this check that fails safe as git's transport list grows.

Existing project rows read as adopted, `origin`, ready, with no error, and the migration reaches no filesystem to decide that. It is the only honest reading of a row written before this decision. The visible consequence is that a registration which previously succeeded against a nonexistent checkout is now refused at registration; the work that used to fail later fails sooner instead.

Separating the base checkout's fetch remote from the clone-side `origin` supports the fork layout directly, and it makes an assumption that was previously implicit into a value that can be seen and changed. The risk it introduces is that the two are easy to confuse in code, which is why the distinction is stated here and not only in a comment.

This decision should be revisited if Ompire needs to manage more than one checkout per project, if roadmaps require the daemon to update a base checkout rather than only read it, or if projects must support forges whose clone URLs do not fit the accepted forms. Any replacement must keep adoption read-only, keep created checkouts confined to a derived location, and keep deletion of a base checkout outside Ompire's authority.

## Alternatives considered

### Keep the checkout unvalidated and let spawn report the problem

Doing nothing was the status quo, and it has the merit that the spawn pipeline's `fetch` step already produces an accurate error. It was rejected because that error arrives after a task, a branch name, and a clone path exist, which makes the operator clean up work that should never have started, and because it can only ever be an error — it cannot offer the alternative of creating the checkout. The vision asks that the rigorous path be the fast path; discovering a misconfigured project on your first task is neither.

### Let Ompire repair an adopted checkout

Adding the missing remote, or fetching it, would have turned most refusals into successes with no operator work at all. It was rejected because it inverts the ownership rule the security model rests on: the base checkout is where the operator does their own work, and a control plane that edits its remotes — even helpfully, even idempotently — is one an operator cannot safely point at a repository they care about. A refusal that names exactly what is missing preserves both the boundary and the operator's ability to fix it in one command.

### Clone directly into the destination path

Cloning straight to `<checkout root>/<name>` would have avoided the staging directory and the rename. It was rejected because an interrupted clone then leaves a partial repository exactly where a valid one belongs. Retry would have to decide whether to delete it — which means Ompire deleting a path that looks like a base checkout, the one thing this decision forbids — and startup reconciliation would have to distinguish a half-clone from a real checkout by inspection. Staging removes the ambiguity instead of managing it.

### Let the operator choose the clone destination

Accepting a destination path in clone mode would have matched adopt mode's flexibility and let an operator place a checkout anywhere. It was rejected because it hands the daemon's one filesystem-creating operation an unbounded target supplied over the network-facing control plane. Deriving the path from a validated root ([ADR-0023](0023-admit-checkout-root-as-bounded-daemon-writable-setting.md)) keeps the blast radius of that authority to one directory the operator chose deliberately.

### Delete the checkout when a project is unregistered

Removing a cloned checkout on project deletion would have made clone mode symmetrical and left no orphaned directories. It was rejected because the operator's uncommitted work, local branches, and stashes may be in that checkout by then, and because it would make "remove this project from the list" a destructive filesystem operation. Ompire's teardown authority stops at the task root, and a leftover directory the operator can delete themselves is strictly better than one Ompire deletes on their behalf.
