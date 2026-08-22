"""Offline contract checks for LOCAL-TESTING.PLAN.md Part 10."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIDELITY = ROOT / "local-test" / "fidelity"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(FIDELITY), *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def test_committed_real_tool_recordings_validate_and_conform() -> None:
    validated = _run("validate", "--all")
    assert validated.returncode == 0, validated.stderr
    assert "VALID" in validated.stdout

    conformed = _run("conform", "--all")
    assert conformed.returncode == 0, conformed.stdout + conformed.stderr
    for tool in ("gh", "omp", "workshop"):
        assert f"PASS {tool} " in conformed.stdout
    for tool in ("llmvet", "my-workshop"):
        assert f"OBSERVE {tool} " in conformed.stdout


def test_fidelity_selfcheck_exercises_public_pipeline() -> None:
    result = _run("selfcheck")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "disabled wrapper passthrough" in result.stdout
    assert "SIGINT forwarding" in result.stdout
    assert "secret audit rejection" in result.stdout
    assert "intentional mismatch diff" in result.stdout
    assert "golden directory unchanged" in result.stdout


def test_outcome_comparison_preserves_behavioral_differences(tmp_path: Path) -> None:
    qa = {
        "task": {
            "id": "11111111-1111-4111-8111-111111111111",
            "updated_at": "2026-08-22T01:02:03Z",
            "status": "shipped",
        },
        "events": [{"type": "ship_finished", "status": "shipped"}],
    }
    local = {
        "events": [{"type": "ship_finished", "status": "shipped"}],
        "task": {
            "status": "shipped",
            "updated_at": "2026-08-23T04:05:06Z",
            "id": "22222222-2222-4222-8222-222222222222",
        },
    }
    qa_path = tmp_path / "qa.json"
    local_path = tmp_path / "local.json"
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    local_path.write_text(json.dumps(local), encoding="utf-8")

    equal = _run("compare-outcomes", str(qa_path), str(local_path))
    assert equal.returncode == 0, equal.stdout + equal.stderr

    local["task"]["status"] = "error"
    local_path.write_text(json.dumps(local), encoding="utf-8")
    different = _run("compare-outcomes", str(qa_path), str(local_path))
    assert different.returncode == 1
    assert '"error"' in different.stdout
    assert '"shipped"' in different.stdout
