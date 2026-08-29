# Register a project

A project is a source repository plus the routing Ompire needs to publish
against it. Tasks are always spawned against a registered project, so this is
the first thing to do after installing.

## What a project holds

| Field | Meaning |
|---|---|
| `name` | Identifier used in paths and URLs. Lowercase letters, digits, and hyphens only. |
| `title` | Human-readable name shown in the UI. |
| `upstream_url` | The repository pull requests are opened against. |
| `fork_url` | Optional. When set, branches are pushed here instead of upstream. |
| `checkout_mode` | `adopt` to use a checkout you already have, `clone` to have Ompire create one. |
| `checkout_path` | Adopt mode only. Your local working checkout. |
| `fetch_remote` | The remote Ompire fetches **in that checkout**. Defaults to `origin`. |

The checkout is the source Ompire clones every task workspace from. You either
point Ompire at one you already have, or let it create one.

## Adopt a checkout you already have

Open the Projects view, add a project, and leave **Use an existing checkout**
selected. Fill in the absolute path, and the fetch remote if the checkout does
not use `origin`.

When you leave the path field Ompire looks at the checkout and reports what it
found: the remotes it has, and — if it cannot be used — exactly why. It fills
the upstream and fork fields from the remotes it detected so you can confirm
or replace them.

Ompire only reads that checkout. It will not add a missing remote, run a
fetch, or touch your branch, index, or working tree. If the remote you named
is not there, fix it in your own repository and try again:

```sh
git -C ~/proj/my-project remote add upstream https://github.com/org/my-project
```

The API form is the same:

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
curl -sS -X POST http://127.0.0.1:4173/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "my-project",
        "title": "My Project",
        "upstream_url": "https://github.com/me/my-project",
        "checkout_path": "/home/me/proj/my-project"
      }'
```

Returns `201` with the stored project, `409` if the name is taken, or `422`
naming what is wrong with the checkout.

Omitting `checkout_path` derives it from your checkout root, which defaults to
`~/proj`. So `my-project` becomes `~/proj/my-project` — and that path must
already be a usable checkout, or registration is refused.

## Let Ompire clone it

Choose **Clone it for me** and give only the URLs. The form shows the
destination it will use, `<checkout root>/<name>`, before you submit.

```sh
curl -sS -X POST http://127.0.0.1:4173/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "my-project",
        "title": "My Project",
        "upstream_url": "https://github.com/org/my-project",
        "checkout_mode": "clone"
      }'
```

The project appears immediately with its card showing `cloning…` and the step
in progress. When the clone finishes the card turns `ready` and you can spawn
against it. If it fails, the card shows which step failed and git's own error,
with **Retry setup** and **Remove project** buttons.

Ompire refuses to start if anything already exists at the destination, and the
destination never holds a half-finished clone — a retry always starts clean.
The clone uses your own git configuration and no stored credential, so a
private repository your `git` cannot reach fails immediately and says so.

To put checkouts somewhere else, change **Checkout root** in Settings before
registering. It applies to the next project you clone; nothing already on disk
moves.

## Using a fork

Set `fork_url` when you cannot push to upstream directly. Ompire pushes task
branches to the fork and opens the pull request against `upstream_url`. Leave
it unset when you have push access to upstream.

In clone mode the fork is added to the new checkout as a second remote named
`fork`.

## When the checkout uses a different remote name

A fork workflow often names the shared repository `upstream` and your own fork
`origin`. Set `fetch_remote` to `upstream` so Ompire refreshes the right one
before building a task workspace. This only affects the base checkout — inside
a task's clone, `origin` always points back at that checkout.

## Renaming and deleting

A project cannot be renamed or deleted while tasks or templates still
reference it. The request fails with `409` and names what is holding it. Clean
up or archive those tasks first. A project whose clone is still running cannot
be removed either.

Removing a project unregisters it. The checkout on disk stays exactly where it
is — including one Ompire cloned for you. Delete it yourself if you want it
gone.

## Next

Tasks spawned against a project use a template for their spawn configuration —
workflow, base branch, branch pattern, model, and prompt preamble. See [Spawn a
task](spawn-a-task.md).
