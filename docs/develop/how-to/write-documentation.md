# Write documentation

Documentation describes the product as it currently works. Every change that
alters observable behavior updates it as part of delivering the change, not as
follow-up work.

There is no separate place for "current behavior". It goes into the
documentation sets themselves, in the category that owns it.

## Choose the audience

Two sets, and no page serves both:

| The behavior is seen by | Set |
|---|---|
| An operator running Ompire | [`docs/use/`](../../use/index.md) |
| A contributor changing Ompire | [`docs/develop/`](../index.md) |

When both audiences need the same information, write it once for its primary
audience and link across from the other. Do not copy it.

## Choose the category

Each set uses the four Diátaxis categories. The category follows from what the
information *is*, not from which feature it belongs to:

| What you are writing | Category |
|---|---|
| Precise current behavior: interfaces, options, states, errors, schemas | `reference/` |
| A goal the reader accomplishes, step by step | `how-to/` |
| A mental model, a concept, or why the product behaves this way | `explanation/` |
| A guaranteed first-run path for someone new | `tutorials/` |
| Why a durable architectural choice was made | [an ADR](write-an-adr.md) |

Most behavior changes land in `reference/`. A change that alters how someone
achieves a goal also touches `how-to/`. A change that alters the model behind
the behavior also touches `explanation/`.

One behavior area may span several pages in several categories. That is the
framework working, not a problem to consolidate away.

## Place the page

Update the pages that already cover the area. Find them from the set's landing
page before writing anything — a page describing the same behavior almost
always exists.

Add a new page only when the change introduces something no existing page has a
home for. Then:

1. Put it in the correct category directory of the correct set.
2. Link it from that set's landing page, under its category.
3. Add it to the `nav` in `mkdocs.yml` at the repository root, in the matching
   section.

## Rules

**Describe the present, not the change.** Documentation explains the resulting
current state permanently. The change's `SPEC.md` explains the intended delta
while the change is active, and then it is deleted.

**Revise, never append.** When behavior changes, update or remove the
superseded statement. Do not append a contradictory delta below it. A document
with two conflicting paragraphs is worse than one that is out of date, because
the reader cannot tell which is current.

**Write it once.** If two pages describe the same behavior, one of them will
drift. Link instead of restating — across categories and across sets alike.

**Rationale belongs in an ADR.** If you find yourself explaining why a design
was chosen rather than what it does, that is either an [ADR](write-an-adr.md)
or an explanation page. Link to it instead.

**Keep it portable.** Pages must read as plain Markdown without the site
generator. See
[ADR-0020](../../adr/0020-author-documentation-as-portable-markdown.md).

## When it gets updated

Updating documentation is part of delivering a change:

- `change-propose` names the exact pages the change will touch, by path, in the
  spec's documentation impact.
- `change-implement` updates them as behavior changes.
- `change-finish` verifies they stand alone without the change files and sit in
  the right category, then deletes the change directory.

A change is not finished while its documentation still depends on artifacts
that are about to be deleted.

See [The change workflow](../explanation/change-workflow.md).
