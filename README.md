# codex-grokbot-mcp

A local, stdio MCP server that asks one configured X Bot two kinds of
read-only question and reads the answer back. There is no code delegation, no
shared lease or control repository. Credentials can be loaded from Vault at startup.

The server posts one webhook packet and waits for one callback. Concurrency is
safe because every request carries its own job ID and the callback inbox stores
exactly one body per job: an identical replay is accepted and a different
second body is rejected.

## Project status

The request path is implemented and covered by local tests: the packet and
result contract, the private SQLite journal, the loopback inbox, the webhook
transport, the coordinator, and the stdio tools. A live webhook and a
published inbox origin remain operator deployment steps. Track bounded work in
[Beads](docs/beads.md).

## Request types

| Type | Question | Answer |
| --- | --- | --- |
| `x_query` | Research public X discussion. | One callback body; sources expected. |
| `ask` | Any quick read-only question. | One callback body in the same shape; sources may be empty. |

Both types use the same v3 envelope and the same result schema. See
[result delivery](docs/result-delivery.md).

## Local MCP tools

Install this package in a Python 3.12+ virtual environment, create an
owner-only configuration file outside the repository from
[`config.example.toml`](config.example.toml), and launch the stdio server:

```sh
python -m codex_grokbot_mcp.server --config /absolute/private/config.toml
```

| Tool | Result |
| --- | --- |
| `grokbot_x_query(query)` | `{"job_id", "state": "queued"}` |
| `grokbot_ask(question)` | `{"job_id", "state": "queued"}` |
| `grokbot_status(job_id)` | `{"job_id", "job_type", "state", "updated_at"}` |
| `grokbot_result(job_id)` | state only, or the validated answer once ready |
| `grokbot_active()` | open local requests |

These five tools are the whole surface. A request is posted once; if the
outcome is ambiguous the job stays `uncertain` and is never resubmitted
automatically. See the [coordinator contract](docs/mcp-coordinator.md), the
[status contract](docs/mcp-status.md), and the
[configuration reference](docs/configuration.md).

For installation and first use in Codex, follow the
[local setup guide](docs/quickstart.md).

## Security boundary

- Version 3 keeps only settings and Vault references in the owner-only config.
  The webhook URL and credentials are loaded into memory with the existing
  Vault CLI session. Version 2 remains available for existing file-based setups.
  Sender and inbox credentials never appear in packets, logs, or the journal.
- Every request is read-only. The packet forbids X writes, credentials,
  redelegation, repositories, and pull requests, and carries only one callback
  URL and one per-job callback token.
- The callback token is sent to the worker and stored only as a hash in the
  inbox. The answer is untrusted text: the coordinator validates it before the
  job becomes `ready`, and Codex verifies anything it acts on.
- The job journal records no prompt text and no credential; it stores state,
  timestamps, the callback digest, and the answer text needed to report a
  result.

See [the security model](docs/security-model.md),
[the single-Bot contract](docs/workers.md), and the
[contribution guide](CONTRIBUTING.md).

## Public development

Only generalized examples and synthetic names belong in this repository.
Before every push, inspect the staged tree and all reachable history, run
Gitleaks, and run `scripts/check_public_hygiene.py` with a private denylist
stored outside the checkout. The denylist must never be committed. Public CI
performs generic secret scanning; the external denylist is a local publication
gate.

```sh
python scripts/check_public_hygiene.py --denylist /path/outside/repo/private-denylist.txt
gitleaks git --redact --log-opts=--all
gitleaks git --staged --redact
```

## License

MIT. See [LICENSE](LICENSE).

The narrow owner-approved legacy metadata exceptions are documented in
[the public identifier policy](docs/public-hygiene.md). Staged content and
new private identifiers remain blocked.
