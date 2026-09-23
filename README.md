# codex-grokbot-mcp

A local, stdio MCP server for bounded coding delegation from Codex to a Grok Bot worker. Codex packages only authorized source snapshots; the worker returns a patch artifact. Codex reviews, applies, and tests accepted changes.

## Project status

Implementation is in progress. The current public tree contains the project process, publication checks, the local source and patch contract, a tested Vault lease client, a scoped GitHub/webhook transport, a private local job journal, a bounded coding-packet builder, and a strict multi-worker configuration loader. A read-only stdio MCP server now exposes job status and worker availability. Delegation and patch retrieval are not yet available. Track bounded work in [Beads](.beads/README.md).

## Read-only MCP status

Install this package in a Python 3.12+ virtual environment, then create an owner-only configuration file outside the repository using [`config.example.toml`](config.example.toml). Launch the local stdio server with:

```sh
python -m codex_grokbot_mcp.server --config /absolute/private/config.toml
```

The server exposes `grokbot_status(job_id)` and `grokbot_active()`. Startup marks interrupted jobs uncertain in the private journal; worker availability is read from the configured Vault KV v2 CAS lease. An uncertain job requires reconciliation even if Vault currently reports an available lease. Vault errors fail the active-worker tool rather than reporting a worker available. See [the status tool contract](docs/mcp-status.md).

## Security boundary

- HashiCorp Vault is required for credentials and shared worker leasing. There is one supported backend.
- Private workspaces require explicit opt-in before source is sent to a private control repository.
- Worker installation tokens are limited to the control repository and required permissions. The worker never receives target repository or Vault credentials.
- The worker's patch is untrusted. Declared changed paths must equal actual patch paths, and each actual path must be allowed by `write_paths`.
- Codex alone applies accepted patches. The worker cannot merge or deploy.

See [the security model](docs/security-model.md), [local contract](docs/local-contract.md), [Vault setup contract](docs/vault-setup.md), [control transport](docs/control-transport.md), [worker guidance](docs/workers.md), [job journal](docs/job-journal.md), [coding packet](docs/coding-packet.md), [configuration](docs/configuration.md), and [contribution guide](CONTRIBUTING.md).

## Public development

Only generalized examples and synthetic names belong in this repository. Before every push, inspect the staged tree and all reachable history, run Gitleaks, and run `scripts/check_public_hygiene.py` with a private denylist stored outside the checkout. The denylist must never be committed. Public CI performs generic secret scanning; the external denylist is a local publication gate.

```sh
python scripts/check_public_hygiene.py --denylist /path/outside/repo/private-denylist.txt
gitleaks git --redact --log-opts=--all
gitleaks git --staged --redact
```

## License

MIT. See [LICENSE](LICENSE).
