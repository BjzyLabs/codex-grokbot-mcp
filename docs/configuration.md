# Local configuration

Copy `config.example.toml` to an owner-only regular file **outside** the
repository. Replace every example value with an exact deployment path. The
loader requires mode `0600`, an HTTPS Vault address, existing CA bundles, and
owner-only AppRole credential files. The SQLite journal's parent directory is
also private. No credential value belongs in the TOML file.

Every worker has its own webhook secret path, worker GitHub App secret path, and
Vault KV v2 lease key. Lease keys must be unique except where multiple
dispatchers intentionally share the same account's Chief of Staff lease. Give
each account's Chief of Staff a distinct key under
`<vault_mount>/GrokBot/Leases/<lease_worker>`. Set `lease_prefix` to
`GrokBot/Leases` and `lease_worker` to the key basename. The stored
`LeaseRecord` remains schema v1 and its `worker` field must equal that
basename. Hermes and MCP must use the same account-specific key and CAS
contract; keep the concrete paths in the private operations configuration. A
caller selects one worker by ID; an absent or busy worker does not cause
automatic fallback. Adding a worker requires verifying its exact Vault fields,
private control-repository installation, supported webhook route, and
shared-dispatcher lease behavior.

Each workspace entry names one canonical absolute directory, an explicit
`enabled = true`, and the worker IDs allowed to see its selected source
snapshots. Missing, disabled, or unlisted workspaces fail closed. The request
still supplies exact read and write paths; workspace opt-in alone does not
authorize sending arbitrary files. Recheck opt-in after a stdio restart.

Bots within one Cursor account [share a cloud computer](https://cursor.com/docs/grok-bot)
with files and app logins. Separate lease keys do not isolate those resources.
Only configure a Bot for source it is authorized to see; use distinct accounts
when the Bots need separate computer and credential boundaries. The config has
no model selector because [Cursor manages Grok Bot model selection](https://cursor.com/docs/grok-bot/settings).

Optional `result_inbox_base_url` and `result_inbox_secret_path` enable v3
callbacks. The Vault KV v2 secret at that path must contain a `requestor_token`
field for the inbox's requestor API. The client uses HTTPS, the configured
`webhook_ca_file`, the exact configured origin, bounded JSON responses, and no
redirects. Omit both keys to keep coding on the control-repository path.

An existing coding worker can be configured for read-only diagnostics with
`account_id`, `diagnostic_chief_worker_id`, and `diagnostic_target_bot` fields.
The target worker's mapping must name a configured Chief of Staff worker with
the same `account_id`; the Chief of Staff must explicitly allow `x_query` and
`worker_diagnostic` in `job_types`. `diagnostic_target_bot` is `coder` or
`devcoder`. These values are an operator-declared account mapping; configure
them only after verifying that both webhook routes reach Bots in the same
Cursor account. The mapped target must allow `coding`. Workers default to
`job_types = ["coding"]`.

`grokbot_delegate(job_type="x_query")` keeps the generic delegate interface,
but it accepts only a configured Chief of Staff worker that is explicitly
referenced by at least one same-account coding worker's diagnostic mapping.
The Chief of Staff must allow both `x_query` and `worker_diagnostic`. A worker
that merely allows `x_query`, or a worker mapped to another account, is
rejected before a job is journaled or any webhook is sent. There is no worker
fallback. X-query dispatch acquires that Chief of Staff account's shared CAS
lease before callback registration and webhook dispatch. A validated terminal
callback releases it; invalid callbacks and ambiguous dispatch, polling, or
release outcomes retain it for reconciliation. Failures proven to happen
before webhook dispatch release only the lease owned by that request.

`grokbot_diagnose(target_job_id)` accepts only an existing recorded coding job.
It sends a fixed read-only question to the configured Chief of Staff, acquires
the same account-specific CAS lease used by MCP X-query and Hermes, and mints
no GitHub token. It waits up to 120 seconds for the Bot-to-Bot response plus 90
seconds for callback delivery, renewing the lease when its heartbeat approaches
expiry. A missing response is recorded as `no_reply` only when Chief of Staff
explicitly reports it in a validated callback. A missing callback is
`uncertain`, since it does not prove that Chief of Staff reached the target
Bot. Validated terminal callbacks release the CoS lease; interrupted or
ambiguous delivery retains it and is never retried automatically.
`grokbot_diagnostic_result(job_id)` returns the durable status and any
validated reply. Replies are advisory evidence and cannot release a coding
worker's lease, retry work, or mark a coding job complete.
