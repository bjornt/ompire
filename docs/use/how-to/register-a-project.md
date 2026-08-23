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
| `checkout_path` | Your local working checkout. Defaults to `checkout_root/name`. |

The checkout at `checkout_path` is the source Ompire clones from. It must
already exist and have `origin` pointing at the repository — Ompire fetches
from it but never modifies it.

## Register through the UI

Open the Projects view and add a project. The name must be a valid slug; the
UI rejects anything else before submitting.

## Register through the API

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
curl -sS -X POST http://127.0.0.1:4173/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "my-project",
        "title": "My Project",
        "upstream_url": "https://github.com/me/my-project"
      }'
```

Returns `201` with the stored project, or `409` if the name is taken.

Omitting `checkout_path` derives it from `checkout_root` in your
configuration, which defaults to `~/proj`. So `my-project` becomes
`~/proj/my-project`.

## Using a fork

Set `fork_url` when you cannot push to upstream directly. Ompire pushes task
branches to the fork and opens the pull request against `upstream_url`. Leave
it unset when you have push access to upstream.

## Renaming and deleting

A project cannot be renamed or deleted while tasks or templates still
reference it. The request fails with `409` and names what is holding it. Clean
up or archive those tasks first.

## Next

Tasks spawned against a project use a template for their spawn configuration —
workflow, base branch, branch pattern, model, and prompt preamble. See [Spawn a
task](spawn-a-task.md).
