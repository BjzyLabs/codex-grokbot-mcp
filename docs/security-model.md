# Security model

The local MCP process is the trusted coordinator. Vault holds webhook credentials, GitHub App credentials, and the KV v2 CAS lease. The control repository transports requests and patch artifacts; it is private whenever a job contains private source. The Grok Bot worker receives only the bounded job packet and a control-repository-scoped token.

Each workspace must explicitly opt in to delegation. A request declares separate `read_paths` and `write_paths`; only selected, authorized source snapshots are sent. Returned artifacts are treated as attacker-controlled. The coordinator checks the job identity, snapshot binding, workspace drift, patch syntax, exact declared-versus-actual changed paths, and membership of all actual paths in `write_paths`. Codex must review, apply, and test a validated patch.

A worker lease is acquired with Vault KV v2 compare-and-swap before dispatch. The same lease contract must be enforced by every dispatcher sharing that worker. Uncertain dispatch or timeout does not free a lease until reconciliation proves a terminal outcome. Jobs are capped at 45 minutes. Durable non-secret state supports reconciliation after a stdio process restart.

There are no alternative credential or lease backends in this project. A failed prerequisite stops dependent work; it does not trigger a fallback transport or weaker validation.

Multiple workers may share one provider account's computer. Separate Vault leases and GitHub App tokens bound jobs and control-repository access; they do not provide filesystem or browser-session isolation between Bots. Only configure a Bot for source it is authorized to see.

## Threats and boundaries

| Threat | Control | Remaining operator responsibility |
| --- | --- | --- |
| A worker sees more source than intended | Explicit workspace opt-in and selected `read_paths`; only snapshots enter the packet | Review each selected file and the provider account's shared-computer access |
| A worker modifies an unapproved file | Exact declared-versus-actual patch path check, `write_paths` limit, and disposable validation workspace | Codex reviews the patch, applies accepted changes, and runs tests |
| Two dispatchers use one worker | Shared Vault KV v2 CAS lease | Prove the guard is installed in every running dispatcher before live dispatch |
| A timeout is mistaken for failure | Durable uncertain state; no automatic POST retry, stale-lease takeover, or lease release | Reconcile the exact Vault record and control branch before operator action |
| A control-repository token grants excess access | Installation token is checked for one repository, required permissions, and expiry; token is revoked after use | Keep the control repository private for private source and restrict its membership |
| The local process stops | Private SQLite journal retains non-secret job state and artifact identity | Reconcile interrupted jobs; source content and token values are deliberately not persisted |

The control repository and the Grok Bot provider still receive the selected
source snapshots. They are within the trust boundary for those specific
files. Treat returned PR text and patch content as untrusted instructions.
