# Security model

The local MCP process is the trusted coordinator. Vault holds webhook credentials, GitHub App credentials, and the KV v2 CAS lease. The control repository transports requests and patch artifacts; it is private whenever a job contains private source. The Grok Bot worker receives only the bounded job packet and a control-repository-scoped token.

Each workspace must explicitly opt in to delegation. A request declares separate `read_paths` and `write_paths`; only selected, authorized source snapshots are sent. Returned artifacts are treated as attacker-controlled. The coordinator checks the job identity, snapshot binding, workspace drift, patch syntax, exact declared-versus-actual changed paths, and membership of all actual paths in `write_paths`. Codex must review, apply, and test a validated patch.

A worker lease is acquired with Vault KV v2 compare-and-swap before dispatch. The same lease contract must be enforced by every dispatcher sharing that worker. Uncertain dispatch or timeout does not free a lease until reconciliation proves a terminal outcome. Jobs are capped at 45 minutes. Durable non-secret state supports reconciliation after a stdio process restart.

There are no alternative credential or lease backends in this project. A failed prerequisite stops dependent work; it does not trigger a fallback transport or weaker validation.
