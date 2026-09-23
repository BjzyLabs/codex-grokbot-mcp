# Local job journal

The local stdio process uses a SQLite journal under a private directory. The
operator supplies an absolute path; the directory must be owned by the current
user with mode `0700`, and the regular database file must have mode `0600`.
Symlinked paths and unexpected schema versions are rejected. The journal uses
SQLite's full synchronous setting and transactional state updates.

Each record contains a job and worker ID, lease owner, local workspace path,
GitHub repository identity, Git HEAD and branch, selected read and write paths,
a source-snapshot digest, control-repository branch and artifact path, state,
and timestamps. It contains no source contents, Vault credential, GitHub token,
App key, or webhook key. The workspace path is local private metadata and must
not be copied into a worker packet or public issue.

A job starts `queued`. State changes are checked against a fixed transition
graph and committed atomically. The process records `dispatching` **before**
the webhook POST. After a restart, the caller must run `reconcile_restart`
before dispatching anything. Jobs whose lease or dispatch may still be active
become `uncertain`; they cannot be dispatched again or moved to `ready` by a
normal transition. An operator or future reconciliation workflow must compare
the saved job ID and worker against the live Vault lease and the exact control
branch before deciding how to proceed. The journal itself never releases a
lease or retries a POST.

Before any returned patch can be accepted, `verify_workspace` recreates the
selected snapshot and compares repository, HEAD, branch, and digest. Drift
moves the job to `conflict` and keeps the saved evidence. A valid snapshot
check does not itself authorize dispatch: per-workspace opt-in must also be
checked from current configuration by the MCP layer.
