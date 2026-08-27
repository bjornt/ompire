# Install Ompire and run your first task

This takes you from nothing to a coding agent working in its own isolated
container, and then to a signed pull request. Expect 20–30 minutes, most of it
waiting for the first container to build.

You will need a GitHub repository you can push to and open pull requests
against. Use a scratch repository the first time.

## 1. Check the prerequisites

Ompire drives tools that live on your machine rather than bundling them. All
four must be present:

```sh
python3 --version          # 3.12 or newer
gh auth status             # authenticated
lxc list                   # container tooling reachable
gpg --list-secret-keys     # at least one signing key
```

If any of these fail, fix it before continuing. Ompire will start without
them, but the task will fail at the step that needs the missing tool.

## 2. Build and start the daemon

```sh
cd frontend && pnpm install && pnpm build
cd ../daemon && uv sync
uv run ompire-daemon
```

The frontend build is required — the daemon serves it as static files.

You should see the daemon start on `http://127.0.0.1:4173`. Leave it running
and open a second terminal for the rest of this tutorial.

## 3. Open the UI

The daemon generated a bearer token on first run. Open the UI once with it:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

The token is stored in your browser, so later visits need no query string.

You should see an empty Tasks view and a daemon chip showing a live
connection. If the chip shows disconnected, the daemon is not running.

## 4. Configure signing

Shipping is blocked until Ompire has a usable signing key. Find yours:

```sh
gpg --list-secret-keys --keyid-format=long
```

Put it in `~/.config/ompire/config.toml`:

```toml
gpg_signing_key = "YOUR_KEY_ID"
```

Restart the daemon — configuration is read once at startup.

Now cache the passphrase, so the GPG chip reports `cached` rather than
`locked`:

```sh
echo test | gpg --clearsign > /dev/null
```

Re-probe from the UI. The chip should turn green. If it says `unknown`, the
key ID in your config does not match a key the agent can see.

## 5. Prepare a checkout

Ompire clones from a local checkout rather than from the network, so the task
starts from refs you already have. Create one:

```sh
mkdir -p ~/proj
git clone https://github.com/you/scratch-repo ~/proj/scratch-repo
```

## 6. Register the project

In the Projects view, add a project:

- **Name:** `scratch-repo` — lowercase, digits, and hyphens only
- **Title:** anything readable
- **Upstream URL:** the GitHub URL you just cloned
- **Checkout path:** leave blank to derive `~/proj/scratch-repo`

Leave the fork URL empty if you can push to the repository directly.

## 7. Spawn a task

In the Spawn view, choose the project, give the task a slug such as
`add-readme-badge`, and write a prompt describing a small, self-contained
change. Something a competent contributor would finish in ten minutes.

Submit, and watch the four spawn steps run:

| Step | What you are waiting for |
|---|---|
| `fetch` | Seconds |
| `clone` | Seconds — it is a local clone |
| `branch` | Instant |
| `workshop` | Minutes on a first run — the container is being built |

The `workshop` step is slow the first time and much faster afterwards. If it
fails, your container tooling is the place to look.

When it completes, Ompire opens the task for you. It has its own clone under
`~/tasks/scratch-repo/add-readme-badge` and an agent running inside its own
container.

## 8. Watch it work — or don't

You are now on the task's detail view, where the agent's output appears as it
works.

The point of Ompire is that you do not have to watch. The task card shows an
attention tier: silent while the agent is working, a badge when it reaches a
turn boundary, a desktop notification if it asks you something, and a
notification with sound if it needs an approval or has failed.

Go do something else. Ompire will tell you when it needs you.

## 9. Review the work

When the agent is idle, start a review from the task detail view.

Ompire opens a review against the host side of the clone — the agent does not
run it and cannot influence the verdict. The review shows the complete task
delta, not just the last commit.

If you want changes, send a review comment back to the agent. It becomes the
agent's next prompt and the session returns to `working`. Repeat until you are
satisfied.

## 10. Ship it

Shipping is two steps, so you see what will be published before it is.

**Draft.** Ompire asks the agent to write the commit message and pull-request
title and body. This is the last thing the agent does.

**Commit.** You edit the drafted text, choose `squash`, and confirm. From here
the daemon does everything itself: signed commit, push, pull request — using
credentials the agent never had access to.

If the commit is refused with a GPG error, your cached passphrase expired.
Re-cache it and try again; nothing was written, so there is nothing to undo.

When it succeeds, the pull-request URL is attached to the task. Check GitHub:
the commit should show as **Verified**.

## 11. Clean up

Once the pull request has landed, clean up the task. The container is removed,
the clone is deleted, and the task is archived — its record and publishing
history remain.

## What you just did

You gave a probabilistic process an isolated workspace, let it work
unsupervised, reviewed its output through a channel it could not influence,
and published the result with a key it never held.

That is the whole idea. Everything else is more workflows, more steps, and
better evidence between them.

## Next

- [Spawn a task](../how-to/spawn-a-task.md) — templates, workflows, and the
  `bugfix` workflow
- [The trust boundary](../explanation/trust-boundary.md) — why the pieces are
  arranged this way, and where the model is currently weaker than intended
- [Configuration](../reference/configuration.md) — every setting
