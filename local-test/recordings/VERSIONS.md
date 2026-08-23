# Local-testing fidelity recordings

Sanitized real-tool recordings used by `local-test/fidelity`. The machine-readable source of truth is `versions.json`.

| Tool | Recorded version | Capture environment | Golden cases |
|---|---|---|---|
| `workshop` | 0.9.5 | QA sandbox host | daemon `launch`; `info-not-project` |
| `my-workshop` | unversioned binary, SHA-256 prefix `2d579731d0132c8c` | QA sandbox host | version/usage contract (observational) |
| `gh` | 2.97.0 (2026-07-31) | QA sandbox host | unknown-repository `pr view` |
| `omp` | 17.4.0 | workstation SDK binary | `config get ask.timeout` |
| `llmvet` | 0.3.0-37-g421d7fd | workstation SDK binary | version contract (observational) |

## Capture on QA

Install wrappers before adding their directory to `PATH`; this records immutable absolute targets and avoids recursive resolution:

```sh
local-test/fidelity install-wrappers "$STATE/record-bin"
export PATH="$STATE/record-bin:$PATH"
```

Normal QA remains uninstrumented. Opt in only for the daemon/run being captured:

```sh
export OMPIRE_RECORD=1
export OMPIRE_RECORD_DIR="$STATE/fidelity-captures"
export OMPIRE_RECORD_REDACTIONS_FILE="$STATE/fidelity-redactions"
```

The redactions file contains one literal sensitive value or sensitive path fragment per line. Never put it under `local-test/recordings/`. The recorder also redacts known credential environment values and persists only the environment allowlist. Run the normal spawn → review → ship QA loop, then validate every capture before copying it off the QA host:

```sh
local-test/fidelity validate "$STATE/fidelity-captures"
```

Promotion is explicit. Pick only a minimal daemon-consumed contract; task prompts and reviewer content are not golden contracts:

```sh
local-test/fidelity promote CAPTURE.json --case CASE \
  --redactions-file "$STATE/fidelity-redactions"
```

Use `--observational` for real `my-workshop` and `llmvet`, which have no fake replay target. Update `versions.json` and this table in the same change.

## Verify locally

```sh
local-test/fidelity validate --all
local-test/fidelity conform --all
local-test/fidelity selfcheck
cd daemon && uv run pytest tests/test_fidelity.py
```

`conform` reconstructs each case below a temporary root and compares normalized exit code, stdout, stderr, and selected filesystem effects. A mismatch prints a unified real-versus-fake diff. `compare-outcomes QA.json LOCAL.json` applies the same normalizer to explicit REST/WebSocket outcome bundles from a scenario run in both environments.

Before a release or after a tool upgrade, run `local-test/fidelity versions --check-real --wrapper-dir "$STATE/record-bin"`. Any mismatch requires a new QA capture and review before the fake is trusted.
