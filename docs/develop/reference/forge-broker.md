# Forge broker

The forge broker gives task tools read-only access to allowlisted GitHub
repositories without putting a GitHub credential in the sandbox. It is a
separate trusted-side process, not a daemon API route: `git` uses its
smart-HTTP surface, and `curl` uses its repository REST surface.

The daemon under test still signs, pushes, and creates pull requests using its
trusted publishing identity. The broker gives the observing agent none of
that authority. See [ADR-0041](../../adr/0041-broker-forge-access-behind-read-only-proxy.md)
for the broker decision and [ADR-0011](../../adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)
for the unchanged publishing boundary.

## Invocation

The module requires Python 3.12 or newer and uses only the standard library.
From the repository root, it can run directly without installing the daemon or
creating its virtual environment:

```sh
python3 daemon/src/ompire_daemon/forge_broker.py \
  --token-file .qa-agent/token \
  --repo 'ompire-test/<sandbox>' \
  --port 4100
```

With the daemon package installed in the selected Python environment, use the
same options with its module entry point:

```sh
python -m ompire_daemon.forge_broker \
  --token-file .qa-agent/token \
  --repo 'ompire-test/<sandbox>' \
  --port 4100
```

The token file is on the **trusted host**, never in the task workspace. Keep
its existing identity-directory protections: directory mode `0700`, token file
mode `0600`. The alternative token source is `OMPIRE_FORGE_TOKEN` in the broker
process's environment. Arrange that environment on the trusted host; do not
paste the token into a command, transcript, or sandbox environment. There is no
token-value command-line option.

| Input | Contract |
|---|---|
| `--token-file PATH` | Read the PAT from this file at startup. Mutually exclusive with `OMPIRE_FORGE_TOKEN`. |
| `OMPIRE_FORGE_TOKEN` | Use this named environment variable instead of a token file. Exactly one token source is required. |
| `--repo OWNER/REPO` | Allow one repository. Required and repeatable; repository comparisons are case-insensitive. |
| `--port PORT` | Listener port; default `4100`. `0` asks the OS for an available port, useful for isolated tests. |
| `--bind ADDRESS` | Default `127.0.0.1`. Only the literal values `127.0.0.1` and `localhost` are accepted; both bind IPv4 `127.0.0.1`. |
| `--help` | Show the supported CLI options. |

For example, add a second repository with another `--repo`, not a comma-separated
value. The same allowlist applies to every request reaching that broker; the
task label does not select a repository or a different policy.

Missing or conflicting token sources, an unreadable or empty token, no
repositories, invalid options, and a bind failure abort startup non-zero with a
diagnostic identifying the problem without printing the credential. Successful
startup prints this line on stdout, using the actual bound port:

```text
forge broker ready http://127.0.0.1:4100
```

Request logs go to stderr, so readiness and audit output can be captured
separately. The broker owns no daemon database or persistent request state.

### Offline upstream hook

`--upstream-base http://127.0.0.1:PORT` is a hidden test hook, not a production
routing option. It accepts only loopback HTTP upstreams. In this mode the full
broker request path, including `/api` or `/git`, goes to the local upstream.
Production requests instead use the fixed HTTPS origins below and strip that
prefix. Tests can therefore exercise the proxy against a local API server or
`git http-backend` without contacting GitHub.

## Listener and tunnel boundary

The listener is plain HTTP on trusted-host loopback. Production upstream
connections are HTTPS to GitHub; the broker does not intercept arbitrary HTTPS
traffic, install a sandbox CA, or perform TLS MITM.

Expose the listener through a Workshop tunnel, not a network-facing port.
There is no client credential on the listener: access to the trusted host's
loopback endpoint or its assigned tunnel gives access to the broker's entire
read-only allowlist. Host-local processes are part of that trust boundary.
`X-Ompire-Task` is not authentication. A task-scoped tunnel must be unavailable
to unrelated tasks and removed with the task environment, as required by
[ADR-0015](../../adr/0015-keep-agent-credentials-behind-narrow-brokers.md).

The consumption stanza below mirrors the existing `pi-auth-gateway` plug and
slot in `workshop.yaml`, with `github` on port `4100`:

```yaml
sdks:
  - name: omp
    plugs:
      pi-auth-gateway:
        interface: tunnel
        endpoint: localhost:4000
      github:
        interface: tunnel
        endpoint: localhost:4100
  - name: system
    slots:
      pi-auth-gateway:
        interface: tunnel
        endpoint: localhost:4000
      github:
        interface: tunnel
        endpoint: localhost:4100
```

Merge these entries into the applicable Workshop declaration; this is not a
complete replacement for its SDK list. The slot side must reach the running
broker on that host. The sandbox uses `http://localhost:4100` after the tunnel
is connected. If the broker runs on another trusted machine, arranging a
protected forward to the slot host is an operator responsibility; exposing the
listener publicly is not a substitute.

This component does not modify this repository's current `workshop.yaml`, start
a broker from `setup-qa-env.sh`, install a service unit, or implement environment
grants. The dogfood-environment integration owns that provisioning and tunnel
wiring. The [QA-loop guide](../how-to/run-the-qa-loop.md) continues to describe
the existing loop, rather than claiming those integrations already exist.

## Endpoint policy

Let `OWNER/REPO` be an allowlisted repository. The broker decides policy before
contacting upstream. Non-allowlisted repositories, ambiguous paths, traversal,
and encoded path delimiters are denied rather than normalized into an allowed
request.

| Method | Broker URL path and query | Result |
|---|---|---|
| `GET`, `HEAD` | `/healthz`, without a query | Local health response; no upstream request or credential/configuration disclosure. |
| `GET`, `HEAD` | `/api/repos/OWNER/REPO` and its repository subpaths | Repository REST read at `https://api.github.com/repos/OWNER/REPO...`; ordinary API query parameters are preserved. |
| `GET`, `HEAD` | `/git/OWNER/REPO[.git]/info/refs?service=git-upload-pack` | Smart-HTTP fetch discovery at `https://github.com/OWNER/REPO.git/info/refs?...`. Only that single service query is allowed. |
| `POST` | `/git/OWNER/REPO[.git]/git-upload-pack`, without a query | Smart-HTTP clone/fetch RPC at `https://github.com/OWNER/REPO.git/git-upload-pack`. |
| Any other combination | Any path | `403` with a generic denial body; no upstream request. |

The optional `.git` suffix on git URLs does not change the repository
allowlist. API reads can retrieve repository metadata, pull requests, commits,
and check runs. The repository-commit endpoint's signature evidence is nested
at **`commit.verification`**, not a top-level `verification` field:

```text
/api/repos/OWNER/REPO/commits/SHA
/api/repos/OWNER/REPO/pulls/NUMBER
/api/repos/OWNER/REPO/commits/SHA/check-runs
```

Account and organization administration, `/api/user`, GraphQL, and every API
write method are denied. Git receive-pack discovery and RPC are denied, so
push is not a supported operation. Other git services, arbitrary repository
file URLs, and extra git discovery query parameters are also denied.

Agent-facing URL shapes are:

```sh
git -c credential.helper= clone \
  'http://localhost:4100/git/ompire-test/<sandbox>.git'
curl -q --fail --show-error \
  'http://localhost:4100/api/repos/ompire-test/<sandbox>/commits/<sha>'
```

Neither command needs a PAT or an `Authorization` header. `curl -q` suppresses
any ambient curl configuration; it must be the first curl option. There is no
`gh` CLI compatibility: use `git` and `curl` for this surface.

## Translation and credential protection

### Requests

Only these agent-supplied headers are forwarded, case-insensitively:

- `Accept`
- `Content-Type`
- `User-Agent`
- `Git-Protocol`

The broker generates upstream transport framing and `Host`; its HTTP client
requests identity encoding rather than forwarding an agent's compression
preference. It supplies `Authorization: Bearer <PAT>` for the API, and HTTP
Basic authentication with username `x-access-token` and the PAT as password
for git. Agent-supplied
`Authorization`, cookies, forwarding headers, and `X-Ompire-Task` are not
forwarded. The task header is consumed only for the local audit log. Smart-HTTP
request bodies are streamed to upstream.

### Responses

The broker relays upstream status and streams the body while scrubbing the
literal PAT and the encoded Basic authorization credential, including secrets
split across upstream read boundaries. The same scrubbing applies to relayed
header values and logs, including error paths.

Only these upstream response headers are eligible for forwarding:

- `Content-Type`
- `Cache-Control`
- `Expires`
- `Pragma`
- `X-GitHub-Request-Id`
- `X-RateLimit-*`

`Location` is a conditional addition **only for a safe, translated redirect**.
The broker does not follow redirects. A redirect must have a destination on the
same upstream origin that is still allowed by the endpoint policy. Its
scrubbed `Location` is rewritten into the broker's `/api/...` or `/git/...`
namespace, so a client following it returns through the broker. A missing,
malformed, different-origin, or policy-denied destination gives `403` instead;
an upstream redirect cannot widen access or send the credential to another
service.

Upstream `Content-Length` is not forwarded: scrubbing can change the number of
bytes sent. Upstream transfer framing is not relayed either. Downstream streamed
responses use connection-close/EOF framing, so consumers must read through EOF,
not expect a preserved length or a reusable response connection. `HEAD` sends
no response body. Cookies and upstream authentication headers are never
forwarded.

## Request audit log

Each request produces one JSON object on a stderr line with these fields:

| Field | Meaning |
|---|---|
| `timestamp` | Request log timestamp. |
| `task` | Best-effort `X-Ompire-Task` value, or `unlabeled` when absent. |
| `method` | Request method. |
| `path` | Requested path, including its query when present, scrubbed before logging. |
| `decision` | Policy allow/deny outcome. |
| `rule` | Matching policy or failure rule. |
| `upstream_status` | Upstream HTTP status, or JSON `null` when no upstream response was obtained. |
| `status` | HTTP status returned by the broker. |

For human correlation a tool can add, for example:

```sh
curl -q --fail --show-error -H 'X-Ompire-Task: qa-observer' \
  'http://localhost:4100/api/repos/ompire-test/<sandbox>'
```

The task label is **spoofable** and never an input to authorization. It is not
proof that a particular task made the request. Logs cover local health checks,
policy denials, upstream responses, and error paths; upstream failures can have
an allowed policy decision while the returned status reports failure. Treat
repository paths and response evidence as potentially sensitive even though
credential material is scrubbed.

## Failure, restart, and rotation

| Observation | Action |
|---|---|
| Startup exits non-zero | Fix the named token source, repository option, bind, or other configuration error, then start again. There is no anonymous fallback. |
| Connection refused at the tunnel endpoint | Check that the broker is running on the slot host and that the tunnel is connected. `/healthz` checks listener reachability, not GitHub authorization. |
| `403` from the broker | Check the method, path, service query, and repository allowlist. Do not supply a sandbox PAT to bypass policy. An unsafe redirect is also denied. |
| Upstream GitHub error | The upstream status and scrubbed response are returned. Inspect the request log and trusted-side PAT/repository permissions or expiry; local health can still succeed. |
| Broker or tunnel failure | Restore the tunnel and restart the broker as needed, then repeat the read or clone/fetch. No request state needs reconciling, and a client operation interrupted mid-transfer must be retried by the client. |

The PAT and repository allowlist are loaded at startup, not hot-reloaded. To
rotate a token, replace its trusted-side file (or update the trusted process
environment), stop the old broker, and start it again. Restart re-reads the
token and loses no durable state. A task ending does not itself require token
rotation because the sandbox has received no credential; suspected exposure on
the trusted side still does.

## QA-host real-service recipe

This is a recipe for an operator's real-service check, **not evidence that a
GitHub smoke has already run**. Use only the `ompire-test` bot's sandbox
repository, never a personal identity or a production repository. The complete
loop and its credential handling are described in the
[QA-loop guide](../how-to/run-the-qa-loop.md).

Start the broker on the trusted QA host with that sandbox allowlisted, using
the invocation above. Run the following on the QA host's loopback endpoint, or
inside a sandbox whose tunnel is explicitly connected. Do **not** source
`.qa-agent/env.sh`, copy the identity directory into the sandbox, or configure
forge authentication for these observing commands.

Choose an existing sandbox PR and a commit published by the daemon under test.
Replace the values below; `outside` must be a different bot sandbox repository
that is **not** in this broker's allowlist:

```sh
base=http://localhost:4100
repo='ompire-test/<sandbox>'
outside='ompire-test/<other-sandbox>'
pr='<existing-pr-number>'
sha='<daemon-published-commit-sha>'
checkout=$(mktemp -d)/sandbox

curl -q --fail --show-error "$base/healthz"
git -c credential.helper= -c core.hooksPath=/dev/null \
  ls-remote "$base/git/$repo.git"
git -c credential.helper= -c core.hooksPath=/dev/null \
  clone "$base/git/$repo.git" "$checkout"

curl -q --fail --show-error -H 'Accept: application/vnd.github+json' \
  -H 'X-Ompire-Task: qa-observer' "$base/api/repos/$repo"
curl -q --fail --show-error -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/pulls/$pr"
curl -q --fail --show-error -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/commits/$sha"
curl -q --fail --show-error -H 'Accept: application/vnd.github+json' \
  "$base/api/repos/$repo/commits/$sha/check-runs"
```

All reads should succeed with the bot's sandbox permissions. In the commit
response inspect `commit.verification.verified` and
`commit.verification.reason`. For a correctly signed, GitHub-verified published
commit expect `true` and `valid`; a broker cannot turn an unverified commit into
a verified one. A successful check-runs read can legitimately contain an empty
`check_runs` array.

Then exercise denials, still only through the broker:

```sh
curl -q --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "$base/api/user"
curl -q --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "$base/api/repos/$outside"
curl -q --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  --request POST "$base/api/repos/$repo"

git -C "$checkout" -c credential.helper= -c core.hooksPath=/dev/null \
  push origin HEAD:refs/heads/forge-broker-denial-probe
```

Each curl prints `403`; the push exits non-zero with an HTTP `403` denial at
receive-pack discovery. Do not change the remote to GitHub or add credentials
if it fails. Inspect the broker's JSON logs for the allowed reads and denied
requests, their rule names and task labels, and `upstream_status: null` for
policy denials. These probes must not publish a branch or contact an outside
repository upstream. Retain only non-secret evidence; never reproduce token
material while inspecting logs or reporting results.
