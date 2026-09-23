# MCP coordinator contract

The local stdio server exposes two mutating lifecycle tools alongside the
read-only status tools. Use an owner-only configuration file outside Git. Vault
is required for webhook credentials, GitHub App credentials, and the shared
KV v2 CAS worker lease. No other backend is supported.

**Live gate:** The coordinator source and local fake-service tests do not prove
the live Grok Bot v2 webhook or artifact contract. Verify that every dispatcher
sharing the selected worker is running the same Vault lease guard before any
live job. A staged canary must prove the live webhook, draft PR, artifact, and
lease behavior before general use.

`grokbot_delegate` accepts an absolute workspace path, one configured
`worker_id`, a goal, exact `read_paths`, exact `write_paths`, one to ten
`acceptance_checks`, and `effort_hint` (`small` or `medium`). The workspace
must explicitly opt in to that worker. The tool records a private job and
returns its ID and `queued` state without waiting for the worker. It sends
only selected source snapshots, never the local workspace path. The effort
hint is advisory and does not change permissions or the 45-minute limit.

The coordinator claims the shared lease before minting a control-repository
installation token or posting to the webhook. It records token expiry and
`dispatching` before the single POST. It never retries an ambiguous POST.
It polls the exact open draft PR and artifact path while renewing the lease.
The artifact must bind to the job and snapshot; its declared changed paths
must equal the parsed patch paths, and every actual path must be allowed by
`write_paths`. The coordinator pins the immutable artifact commit and
canonical content digest before revoking the token, releasing the lease, and
marking the job `ready`.

`grokbot_status(job_id)` reports progress. `grokbot_result(job_id)` returns
state only until the job is `ready`. For a ready job it rechecks current
workspace opt-in and source state, reads the exact pinned artifact commit with
a newly scoped short-lived token, checks the digest and patch again, and
returns the patch, summary, and actual changed paths. Codex must review,
apply, and test the patch; this server never applies it to real source.

An ambiguous lease operation, webhook outcome, timeout, renewal failure, or
interrupted process leaves the job `uncertain` and retains the lease for
explicit reconciliation. A restart also marks a queued job uncertain because
it may have claimed a lease immediately before stopping. Do not retry the
webhook or release the lease based on journal state alone. Compare the saved
job ID and worker with the exact live Vault lease and control branch first.
Invalid worker output or changed source becomes `conflict`. Neither state
is silently retried or substituted with another worker.
