# codex-grokbot-mcp

A local, stdio MCP server for bounded coding delegation from Codex to a Grok Bot worker. Codex packages only authorized source snapshots; the worker returns a patch artifact. Codex reviews, applies, and tests accepted changes.

## Project status

Implementation is in progress. The current public tree contains the project process, publication checks, and the local source and patch contract. It does not yet provide a working MCP server. Track bounded work in [Beads](.beads/README.md).

## Security boundary

- HashiCorp Vault is required for credentials and shared worker leasing. There is one supported backend.
- Private workspaces require explicit opt-in before source is sent to a private control repository.
- Worker installation tokens are limited to the control repository and required permissions. The worker never receives target repository or Vault credentials.
- The worker's patch is untrusted. Declared changed paths must equal actual patch paths, and each actual path must be allowed by `write_paths`.
- Codex alone applies accepted patches. The worker cannot merge or deploy.

See [the security model](docs/security-model.md), [local contract](docs/local-contract.md), and [contribution guide](CONTRIBUTING.md).

## Public development

Only generalized examples and synthetic names belong in this repository. Before every push, inspect the staged tree and all reachable history, run Gitleaks, and run `scripts/check_public_hygiene.py` with a private denylist stored outside the checkout. The denylist must never be committed. Public CI performs generic secret scanning; the external denylist is a local publication gate.

```sh
python scripts/check_public_hygiene.py --denylist /path/outside/repo/private-denylist.txt
gitleaks git --redact --log-opts=--all
gitleaks git --staged --redact
```

## License

MIT. See [LICENSE](LICENSE).
