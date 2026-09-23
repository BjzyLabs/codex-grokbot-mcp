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
