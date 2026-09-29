# Read-only MCP status

The package's stdio entry point is `python -m codex_grokbot_mcp.server --config /absolute/private/config.toml`. The configuration file remains outside Git and is readable only by its owner. The server opens the private SQLite journal, marks interrupted jobs `uncertain`, and then serves MCP through the official Python SDK.

`grokbot_status` accepts a `job_id` and returns its ID, worker ID, state, and update time. An unknown ID returns `state: not_found`. The response contains no source snapshot, workspace path, repository name, credential, token, control branch, or artifact path.

`grokbot_active` returns configured workers and nonterminal local jobs. A worker is `busy` when its Vault lease is active, `available` when the CAS-protected lease is free and no uncertain local job exists, or `reconciliation_required` when a local uncertain job remains despite a free lease. A Vault or journal failure returns an MCP tool error; it never implies availability. This tool reads Vault metadata and lease records but makes no lease changes.

`grokbot_inspect_job` recomputes read-only journal, Vault lease, and GitHub PR evidence for a recorded coding job. Its GitHub search is bound to the exact control-repository head branch and scans open and closed PRs, including merged PRs. It checks at most five pages of 100 results and returns at most 25 exact matches; `search_complete` and `matches_truncated` make those bounds visible. Each match includes lifecycle state, draft status, merge time, head SHA, and base repository/ref. If the page cap is reached, a no-match result is explicitly marked incomplete. Artifact identity checks remain preliminary. This tool does not accept a coding artifact, validate its patch, or prove the Bot is idle or the job is complete. Coding acceptance continues to require the coordinator's separate open draft PR and artifact validation path.

These tools are observational. They do not dispatch a job, retry an uncertain webhook request, release a lease, retrieve a patch, or apply worker output. See [the coordinator contract](mcp-coordinator.md) for the separate delegation and result tools.
