# Read-only MCP status

The package's stdio entry point is `python -m codex_grokbot_mcp.server --config /absolute/private/config.toml`. The configuration file remains outside Git and is readable only by its owner. The server opens the private SQLite journal, marks interrupted jobs `uncertain`, and then serves MCP through the official Python SDK.

`grokbot_status` accepts a `job_id` and returns its ID, worker ID, state, and update time. An unknown ID returns `state: not_found`. The response contains no source snapshot, workspace path, repository name, credential, token, control branch, or artifact path.

`grokbot_active` returns configured workers and nonterminal local jobs. A worker is `busy` when its Vault lease is active, `available` when the CAS-protected lease is free and no uncertain local job exists, or `reconciliation_required` when a local uncertain job remains despite a free lease. A Vault or journal failure returns an MCP tool error; it never implies availability. This tool reads Vault metadata and lease records but makes no lease changes.

These tools are observational. They do not dispatch a job, retry an uncertain webhook request, release a lease, retrieve a patch, or apply worker output. The delegation and result tools will be added only with the complete coordinator and its tests.
