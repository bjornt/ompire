"""The authoring REST surface: create, import, duplicate, validate, save,
export, and archive over HTTP (ADR-0031).

What these tests hold is the authority boundary. Authoring is data: nothing
here executes a command, fetches a URL, reads a server path named in a
document, or grants any review, signing, or publishing power. Validation is a
read; only an explicit executable save changes what a name would launch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

MINIMAL = """
format: 1
name: {name}
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "{body}"
"""


def minimal(name: str = "custom", body: str = "do it") -> str:
    return MINIMAL.format(name=name, body=body)


@pytest.fixture
def headers(auth_headers: dict[str, str]) -> dict[str, str]:
    return auth_headers


def create(client: TestClient, headers, **body):
    return client.post("/api/workflow-library", headers=headers, json=body)


def version(client: TestClient, headers, name: str) -> int:
    return client.get(f"/api/workflow-library/{name}", headers=headers).json()["entry"][
        "version"
    ]


def save_revision(client: TestClient, headers, name: str, yaml_text: str):
    return client.post(
        f"/api/workflow-library/{name}/revisions",
        headers=headers,
        json={"yaml": yaml_text, "expected_version": version(client, headers, name)},
    )


# --- listing and reading ------------------------------------------------------


def test_the_library_lists_what_exists_and_the_catalog_what_can_launch(
    client: TestClient, headers
) -> None:
    assert create(client, headers, name="custom").status_code == 201
    library = client.get("/api/workflow-library", headers=headers).json()
    assert [e["name"] for e in library] == ["bugfix", "custom", "single-step"]
    # `custom` is a draft, so it exists but cannot be selected.
    catalog = client.get("/api/workflows", headers=headers).json()
    assert [w["name"] for w in catalog] == ["bugfix", "single-step"]


def test_a_builtin_reads_as_its_packaged_text_and_cannot_be_edited(
    client: TestClient, headers
) -> None:
    detail = client.get("/api/workflow-library/single-step", headers=headers).json()
    assert detail["entry"]["origin"] == "builtin"
    assert detail["entry"]["has_draft"] is False
    # The example an operator reads before duplicating it.
    assert "name: single-step" in detail["draft_yaml"]
    refused = client.put(
        "/api/workflow-library/single-step/draft",
        headers=headers,
        json={"yaml": "x", "expected_version": detail["entry"]["version"]},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "workflow_builtin_read_only"


def test_an_unknown_entry_is_a_404(client: TestClient, headers) -> None:
    assert client.get("/api/workflow-library/nope", headers=headers).status_code == 404


# --- creating, importing, duplicating -----------------------------------------


def test_creating_without_a_source_opens_a_valid_starter(
    client: TestClient, headers
) -> None:
    """The starter has to be a real format-2 definition, not a sketch: an
    operator's first Validate must succeed."""
    detail = create(client, headers, name="starter").json()
    assert detail["entry"]["current_revision"] is None
    checked = client.post(
        "/api/workflow-library/validate",
        headers=headers,
        json={"yaml": detail["draft_yaml"], "name": "starter"},
    )
    assert checked.status_code == 200
    assert checked.json()["format"] == 2
    assert [s["name"] for s in checked.json()["descriptor"]["steps"]] == [
        "work",
        "finish",
    ]


def test_importing_text_is_an_ordinary_draft(client: TestClient, headers) -> None:
    detail = create(client, headers, name="imported", yaml=minimal("imported")).json()
    assert detail["draft_yaml"] == minimal("imported")
    # Importing did not save an executable revision or launch anything.
    assert detail["entry"]["current_revision"] is None
    assert detail["revisions"] == []


def test_duplicating_a_builtin_copies_the_procedure_under_a_new_name(
    client: TestClient, headers
) -> None:
    """Built-ins are read-only examples, and duplication is their editing path.

    The copy is the same procedure renamed, which makes it a genuinely
    different document — so it gets its own content revision rather than
    sharing the built-in's.
    """
    source = client.get("/api/workflows", headers=headers).json()
    bugfix = next(w for w in source if w["name"] == "bugfix")
    detail = create(
        client, headers, name="my-bugfix", source_revision=bugfix["revision"]
    ).json()
    assert "name: my-bugfix" in detail["draft_yaml"]
    saved = save_revision(client, headers, "my-bugfix", detail["draft_yaml"]).json()
    assert saved["entry"]["current_revision"] != bugfix["revision"]
    assert saved["entry"]["current_format"] == 2
    assert saved["entry"]["available"] is True


def test_creating_refuses_two_sources_and_a_taken_name(
    client: TestClient, headers
) -> None:
    ambiguous = create(client, headers, name="x", yaml="a", source_revision="sha256:b")
    assert ambiguous.status_code == 422
    taken = create(client, headers, name="bugfix")
    assert taken.status_code == 409
    assert taken.json()["detail"]["reason"] == "workflow_name_taken"
    assert taken.json()["detail"]["origin"] == "builtin"


def test_an_unknown_request_field_is_refused(client: TestClient, headers) -> None:
    assert create(client, headers, name="x", yamll="oops").status_code == 422


def test_a_bad_name_comes_back_as_a_readable_message(client: TestClient, headers) -> None:
    """A refusal an editor can show the operator.

    Validating the name in a Pydantic field validator would hand this to
    FastAPI's own request-validation layer, whose `detail` is a list of
    error objects with no message a client renders — so the operator would
    see the status code instead of being told what is wrong with the name.
    """
    response = create(client, headers, name="My Workflow")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "lowercase alphanumerics and hyphens" in detail


# --- validation ---------------------------------------------------------------


def test_validation_locates_the_problem_and_saves_nothing(
    client: TestClient, headers
) -> None:
    create(client, headers, name="custom", yaml=minimal())
    broken = minimal().replace("kind: agent", "kind: nonsense")
    response = client.post(
        "/api/workflow-library/validate", headers=headers, json={"yaml": broken}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "workflow_document_invalid"
    assert detail["location"] == "steps[0].kind"
    assert "one of" in detail["message"]
    # The submitted text is never echoed back.
    assert broken not in str(detail)
    assert client.get("/api/workflow-library/custom", headers=headers).json()["entry"][
        "current_revision"
    ] is None


def test_a_yaml_syntax_error_carries_its_source_position(
    client: TestClient, headers
) -> None:
    response = client.post(
        "/api/workflow-library/validate",
        headers=headers,
        json={"yaml": "format: 1\nname: x\n  bad: indentation\n"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["line"] >= 1
    assert detail["column"] >= 1


def test_unsafe_yaml_constructs_are_refused_and_execute_nothing(
    client: TestClient, headers
) -> None:
    """The loader is a closed grammar over data. There is no Python
    constructor, no template engine, no include, and no path it will open."""
    for text in (
        "!!python/object/apply:os.system ['touch /tmp/pwned']\n",
        "format: 1\nname: &a x\nsessions: [*a]\n",
        "format: 1\nname: x\n<<: {a: 1}\n",
    ):
        response = client.post(
            "/api/workflow-library/validate", headers=headers, json={"yaml": text}
        )
        assert response.status_code == 422, text


def test_validation_binds_to_an_entry_identity(client: TestClient, headers) -> None:
    """An editor is told about a rename before it saves, not after."""
    create(client, headers, name="custom", yaml=minimal())
    response = client.post(
        "/api/workflow-library/validate",
        headers=headers,
        json={"yaml": minimal(name="renamed"), "name": "custom"},
    )
    assert response.status_code == 422
    assert "identity" in response.json()["detail"]


# --- executable saves and conflicts -------------------------------------------


def test_a_saved_revision_becomes_the_launch_choice(client: TestClient, headers) -> None:
    create(client, headers, name="custom", yaml=minimal())
    saved = save_revision(client, headers, "custom", minimal()).json()
    assert saved["entry"]["available"] is True
    catalog = client.get("/api/workflows", headers=headers).json()
    assert [w["name"] for w in catalog] == ["bugfix", "custom", "single-step"]
    # Saving started nothing.
    assert client.get("/api/tasks", headers=headers).json() == []


def test_a_failed_executable_save_leaves_the_last_one_in_place(
    client: TestClient, headers
) -> None:
    create(client, headers, name="custom", yaml=minimal())
    good = save_revision(client, headers, "custom", minimal()).json()
    refused = save_revision(client, headers, "custom", "format: 1\nname: custom\n")
    assert refused.status_code == 422
    entry = client.get("/api/workflow-library/custom", headers=headers).json()["entry"]
    assert entry["current_revision"] == good["entry"]["current_revision"]
    assert entry["version"] == good["entry"]["version"]


def test_a_stale_save_is_refused_with_the_current_version(
    client: TestClient, headers
) -> None:
    create(client, headers, name="custom", yaml=minimal())
    stale = version(client, headers, "custom")
    client.put(
        "/api/workflow-library/custom/draft",
        headers=headers,
        json={"yaml": "tab one", "expected_version": stale},
    )
    response = client.put(
        "/api/workflow-library/custom/draft",
        headers=headers,
        json={"yaml": "tab two", "expected_version": stale},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "workflow_version_conflict"
    assert detail["current_version"] == stale + 1
    # Nothing changed: tab one's text is still what is saved.
    assert (
        client.get("/api/workflow-library/custom", headers=headers).json()["draft_yaml"]
        == "tab one"
    )


# --- export and portability ---------------------------------------------------


def test_an_export_reimports_as_the_same_procedure(client: TestClient, headers) -> None:
    """A round trip through the file an operator keeps.

    Export, create a new entry from that exact text under a new name, and the
    only difference is the name — which is exactly why the copy has its own
    content identity.
    """
    create(client, headers, name="custom", yaml=minimal())
    saved = save_revision(client, headers, "custom", minimal()).json()
    revision = saved["entry"]["current_revision"]

    exported = client.get(
        f"/api/workflows/revisions/{revision}/yaml", headers=headers
    ).json()
    assert exported["revision"] == revision

    reimported = create(client, headers, name="copy", yaml=exported["yaml"]).json()
    # The same name, so the same identity: re-importing an unchanged export is
    # the same procedure, not a new one.
    again = save_revision(client, headers, "custom", exported["yaml"]).json()
    assert again["entry"]["current_revision"] == revision
    assert len(again["revisions"]) == 1
    assert reimported["draft_yaml"] == exported["yaml"]


def test_exporting_an_unknown_revision_is_a_404(client: TestClient, headers) -> None:
    response = client.get(
        "/api/workflows/revisions/sha256:0000/yaml", headers=headers
    )
    assert response.status_code == 404


# --- archive and restore ------------------------------------------------------


def test_archiving_removes_an_entry_from_launch_choices_only(
    client: TestClient, headers
) -> None:
    create(client, headers, name="custom", yaml=minimal())
    save_revision(client, headers, "custom", minimal())
    archived = client.post(
        "/api/workflow-library/custom/archive",
        headers=headers,
        json={"expected_version": version(client, headers, "custom")},
    ).json()
    assert archived["entry"]["archived"] is True
    assert archived["entry"]["descriptor"] is None
    assert archived["draft_yaml"] == minimal()
    assert [w["name"] for w in client.get("/api/workflows", headers=headers).json()] == [
        "bugfix",
        "single-step",
    ]
    # Still listed in the library, and still readable.
    assert "custom" in [
        e["name"] for e in client.get("/api/workflow-library", headers=headers).json()
    ]

    restored = client.post(
        "/api/workflow-library/custom/restore",
        headers=headers,
        json={"expected_version": version(client, headers, "custom")},
    ).json()
    assert restored["entry"]["available"] is True


def test_authoring_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/workflow-library").status_code == 401
    assert client.post("/api/workflow-library", json={"name": "x"}).status_code == 401
    assert (
        client.post("/api/workflow-library/validate", json={"yaml": "x"}).status_code
        == 401
    )


# --- launch consistency (ADR-0026, ADR-0031) ----------------------------------


def _launch_body(workflow: str = "custom", slug: str = "fix-bug") -> dict:
    return {
        "project_name": "demo",
        "workflow_name": workflow,
        "slug": slug,
        "prompt": "do the thing",
    }


def _saved_custom(client: TestClient, headers, body: str = "do it") -> dict:
    create(client, headers, name="custom", yaml=minimal(body=body))
    return save_revision(client, headers, "custom", minimal(body=body)).json()


def test_a_draft_only_entry_cannot_be_launched_even_through_the_api(
    client: TestClient, headers, demo_project: dict
) -> None:
    """Not just hidden from the form: a direct call is refused on the workflow
    field, because a draft is text nobody validated."""
    create(client, headers, name="custom", yaml=minimal())
    response = client.post(
        "/api/tasks/preview", headers=headers, json=_launch_body()
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("workflow_name:")
    assert "draft" in detail


def test_an_executable_save_between_preview_and_acceptance_is_refused(
    client: TestClient, headers, demo_project: dict
) -> None:
    """The operator reviewed a procedure, not a name. Editing what that name
    means invalidates the review — creation is refused, not retried under the
    new definition."""
    _saved_custom(client, headers)
    preview = client.post(
        "/api/tasks/preview", headers=headers, json=_launch_body()
    ).json()

    save_revision(client, headers, "custom", minimal(body="something else"))

    response = client.post(
        "/api/tasks",
        headers=headers,
        json={**_launch_body(), "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "preview_changed"
    assert client.get("/api/tasks", headers=headers).json() == []


def test_archiving_between_preview_and_acceptance_refuses_the_launch(
    client: TestClient, headers, demo_project: dict
) -> None:
    """An archived entry cannot be accepted through a stale UI. The refusal
    names the workflow field, and no task or spawn job is created."""
    _saved_custom(client, headers)
    preview = client.post(
        "/api/tasks/preview", headers=headers, json=_launch_body()
    ).json()

    client.post(
        "/api/workflow-library/custom/archive",
        headers=headers,
        json={"expected_version": version(client, headers, "custom")},
    )

    response = client.post(
        "/api/tasks",
        headers=headers,
        json={**_launch_body(), "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("workflow_name:")
    assert "archived" in detail
    assert client.get("/api/tasks", headers=headers).json() == []


def test_a_draft_edit_and_an_unrelated_entry_leave_a_preview_valid(
    client: TestClient, headers, demo_project: dict
) -> None:
    """Only a change to what *this* launch would execute invalidates a review.

    Editing this entry's draft changes nothing about what Spawn would run, and
    a different workflow is not this launch at all.
    """
    _saved_custom(client, headers)
    create(client, headers, name="unrelated", yaml=minimal("unrelated"))
    preview = client.post(
        "/api/tasks/preview", headers=headers, json=_launch_body()
    ).json()

    client.put(
        "/api/workflow-library/custom/draft",
        headers=headers,
        json={
            "yaml": "# still thinking\n" + minimal(),
            "expected_version": version(client, headers, "custom"),
        },
    )
    save_revision(client, headers, "unrelated", minimal("unrelated"))

    response = client.post(
        "/api/tasks",
        headers=headers,
        json={**_launch_body(), "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 202, response.text


def test_an_accepted_task_keeps_its_revision_across_edit_and_archive(
    client: TestClient, headers, demo_project: dict
) -> None:
    _saved_custom(client, headers)
    accepted = client.post(
        "/api/tasks",
        headers=headers,
        json={
            **_launch_body(),
            "preview_token": client.post(
                "/api/tasks/preview", headers=headers, json=_launch_body()
            ).json()["preview_token"],
        },
    )
    assert accepted.status_code == 202, accepted.text
    task_id = accepted.json()["id"]
    pinned = client.get(f"/api/tasks/{task_id}", headers=headers).json()[
        "execution_inputs"
    ]["workflow_binding"]["revision"]

    save_revision(client, headers, "custom", minimal(body="a later edit"))
    client.post(
        "/api/workflow-library/custom/archive",
        headers=headers,
        json={"expected_version": version(client, headers, "custom")},
    )

    detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
    assert detail["execution_inputs"]["workflow_binding"]["revision"] == pinned
    # And the pinned definition is still readable, which is what makes the run
    # explainable after the library moved on.
    assert (
        client.get(f"/api/workflows/revisions/{pinned}", headers=headers).status_code
        == 200
    )
