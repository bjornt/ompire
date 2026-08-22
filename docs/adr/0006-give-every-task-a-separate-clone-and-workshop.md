# ADR 0006: Give every task a separate clone and Workshop container

- Status: Proposed
- Date: 2026-07-19

## Context

Ompire runs repository code, dependency scripts, tools, and coding agents that the trusted control plane must treat as untrusted. Concurrent tasks for one project must not contend for a working tree, index, refs, process namespace, or container lifecycle. The operator's primary checkout must also remain usable while tasks create commits, install dependencies, and modify arbitrary files.

A Git worktree provides a separate working tree and index, but it is not self-contained. Its Git directory points back to metadata in the primary repository using a host path, so mounting only the worktree at the container's project path breaks Git. Making it work would require exposing the primary repository's Git metadata to the container, including writable refs and object storage shared with every task.

The implemented alternative creates a local Git clone for each task. The clone has its own working tree, index, refs, configuration, and Git directory, and can therefore be mounted at a stable container path without also mounting the primary checkout. Because the clone source is local, Git normally hardlinks object files when the source and destination are on the same filesystem. This makes creation fast and avoids duplicating existing object data. A task-specific Workshop container then gives the task a separate filesystem view and process lifecycle.

The clone behavior was recorded on 2026-07-18 and the complete clone-and-container lifecycle on 2026-07-19. This ADR uses the latter date for the combined decision.

The implementation and the desired security model do not yet agree on the meaning of isolation. Hardlinked Git objects are separate directory entries for shared inodes. Ordinary Git operations treat existing objects as immutable, but an unrestricted process can modify an object file in place and thereby corrupt the corresponding object in the primary checkout. A hardlink clone therefore separates Git control metadata and normal task operations, but it does not provide the stronger guarantee that the sandbox has no writable path to any storage used by the primary checkout. The Workshop's credential, mount, network, and broker policies also require separate decisions. This ADR remains proposed until the shared-inode conflict is resolved or the durable security requirement is narrowed explicitly.

## Decision

Ompire gives every task its own disposable Git clone and its own Workshop container:

- The daemon constructs each clone below its configured task root and refuses any resolved clone path outside that root.
- The clone is created from the registered local checkout using Git's local-clone behavior. It has a complete Git directory and does not use the primary repository's worktree metadata.
- The task receives its own branch and never reuses a pre-existing clone directory.
- The daemon launches one task-specific Workshop from that clone. Agent and workflow commands execute inside that Workshop against the clone mounted as the task project.
- Cleanup removes the Workshop before deleting the clone. Failure to remove a container aborts clone deletion so the daemon does not orphan container state. Clone deletion is confined to the configured task root.

The invariant is that tasks never share a working tree, index, refs, or Workshop container, and the primary repository's Git directory is never mounted into a task container. Task filesystem deletion must remain confined to the daemon-owned task root, and container teardown must precede clone deletion.

Acceptance additionally requires resolving the hardlinked-object conflict described above. If the security invariant remains that no writable storage is shared with the primary checkout, clone creation must use independent object storage, such as a copy-on-write clone whose writes cannot affect the source or a Git clone with hardlinking disabled.

## Consequences

Each task can modify files, stage changes, create commits, and run tools without changing another task's working tree, index, branch refs, or container processes. Independent tasks can run concurrently against one project. The clone is self-contained at its container mount path, so Git commands do not require a second mount exposing the primary repository's metadata.

Local cloning is fast and space-efficient because the initial object database is normally hardlinked while the worktree and Git control metadata are distinct. New commits and objects are written into the task clone, and deleting the clone leaves no worktree registration or ref bookkeeping in the primary repository.

Hardlink efficiency carries a security cost: the clone and primary checkout may share object-file inodes. Git's normal append-only treatment of objects reduces accidental risk but is not a security boundary against an unrestricted process. Until object storage is made independent, Ompire must not claim that clone-per-task alone prevents all sandbox writes from affecting the primary checkout. Disabling hardlinks increases clone time and disk use; a copy-on-write mechanism would need explicit platform guarantees and verification.

A Workshop per task consumes more memory, storage, startup time, and host container capacity than shared host execution or a shared container. Spawn must surface the comparatively slow container-launch step, and operational limits may be needed as concurrency grows. The benefit is a task-scoped process and filesystem lifecycle that can be inspected, recovered, or removed without selecting processes belonging to other tasks.

Cleanup is deliberately fail-closed. If Workshop removal fails or its state cannot be handled safely, the clone remains for operator recovery and the task is not archived. This can leave failed tasks consuming disk and container resources, but it avoids deleting the project directory from beneath a live container. Path confinement prevents the cleanup operation from recursively deleting an arbitrary operator path; changes to task-root resolution or deletion must preserve that defense.

The dedicated clone and container are necessary but not sufficient for the complete sandbox security model. Mount allowlists, credential delivery, network egress, resource controls, privileged service brokers, and host-side publishing authority remain separate boundaries. Those mechanisms must not treat the existence of a task-specific Workshop as proof that secrets or host capabilities are absent.

This decision should be revisited through a superseding ADR if Workshop is replaced, if clone startup or storage cost becomes operationally prohibitive, or if a different workspace mechanism can remain self-contained inside the sandbox while providing equivalent task separation and cleanup safety. It cannot become accepted while unrestricted task processes can mutate storage shared with the primary checkout under the stronger no-shared-writable-storage security requirement.

## Alternatives considered

### Git worktree per task

Worktrees are faster and use less disk while giving each task a separate checkout and index. They were rejected because their administrative Git directory and refs remain in the primary repository. A worktree mounted alone at the container's project path cannot resolve its host-path Git metadata; mounting that metadata would expose shared writable repository state to the task and couple cleanup to worktree bookkeeping.

### Fully independent Git clone

Disabling local-clone hardlinks gives each task independent object-file inodes while preserving the separate working tree, index, refs, Git directory, and container mount. It costs more clone time and disk space, especially for large repositories. This is the required alternative if Ompire retains the stronger rule that an unrestricted sandbox must have no writable storage shared with the primary checkout; the current hardlink optimization cannot satisfy that rule by convention alone.

### Shared checkout or shared Workshop

Running tasks in the registered checkout or multiplexing them through one container would reduce setup time and resource consumption. It was rejected because tasks would contend for files, indexes, refs, dependencies, and processes. One failed or hostile task could alter another task's inputs or outputs, cleanup could not target one task safely, and parallel execution would require coordination around shared mutable state rather than isolation by construction.
