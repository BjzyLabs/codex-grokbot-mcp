# Local configuration

Copy `config.example.toml` to an owner-only regular file **outside** the
repository. Replace every example value with an exact deployment path. The
loader requires mode `0600`, an HTTPS Vault address, existing CA bundles, and
owner-only AppRole credential files. The SQLite journal's parent directory is
also private. No credential value belongs in the TOML file.

Every worker has its own webhook secret path, worker GitHub App secret path, and
Vault KV v2 lease key. Lease keys must be unique. A caller selects one worker
by ID; an absent or busy worker does not cause automatic fallback. Adding a
worker requires verifying its exact Vault fields, private control-repository
installation, supported webhook route, and shared-dispatcher lease behavior.

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
Cursor account. Workers default to `job_types = ["coding"]`.

`grokbot_diagnose(target_job_id)` accepts only an existing recorded coding job.
It sends a fixed read-only question to the configured Chief of Staff, creates
no coding lease, and mints no GitHub token. It waits up to 120 seconds for the
Bot-to-Bot response plus 90 seconds for callback delivery. A missing response
is recorded as `no_reply` only when Chief of Staff explicitly reports it in a
validated callback. A missing callback is `uncertain`, since it does not prove
that Chief of Staff reached the target Bot. Interrupted or ambiguous delivery
is never retried automatically. `grokbot_diagnostic_result(job_id)` returns
the durable status and any validated reply. Replies are advisory evidence and
cannot release a lease, retry work, or mark a coding job complete.
