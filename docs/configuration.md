# Local configuration

An owner-only TOML file configures the requestor. Version 3 stores settings and
Vault references only; the webhook URL, sender key, and inbox requestor token
are loaded into memory at startup through the installed `vault` CLI. Version 2
remains supported for existing installations with credentials in their private
configuration file. New installations should use version 3.

Copy `config.example.toml` outside Git, replace the synthetic values, and set
mode `0600`. The loader rejects a file owned by another user, a symlink, a
missing or unknown key, a nested table, or any other mode. Never include real
Vault paths or endpoint URLs in the public repository.

## Version 3: Vault-backed configuration

| Key | Rule |
| --- | --- |
| `version` | Integer `3`. |
| `job_database` | Absolute path to the private SQLite journal. |
| `inbox_base_url` | Canonical HTTPS origin: no path, query, fragment, userinfo, or IP address. |
| `vault_webhook_path` | Exact KV v2 mount and secret path; fields `webhook_url` and `sender_key`. |
| `vault_inbox_path` | Exact KV v2 mount and secret path; field `requestor_token`. |

The CLI must be installed and available on the MCP process's `PATH`. Set
`VAULT_ADDR` to the existing HTTPS Vault endpoint and supply the established
CA configuration, such as `VAULT_CACERT`, where needed. TLS verification cannot
be disabled. The MCP process uses the CLI's existing authentication; it never
logs in, creates credentials, changes policies, or writes secrets to disk.

Startup verifies that the existing Vault session is non-root with a positive
TTL, then reads the two configured entries. Each CLI call has a 20-second
timeout. Missing fields, unsafe credentials, invalid responses, failed reads,
and expired or root sessions stop startup before a job is created or posted.
Captured CLI output and exception details are not included in errors. Secrets
and the private webhook URL are excluded from the configuration's `repr`.

Credentials are loaded once per process; restart the MCP server after an
approved rotation. This does not change how the Vault CLI itself authenticates
or stores its session. Use your established short-lived session management;
do not replace these credentials with a long-lived Vault token on disk.

## Version 2: existing file-based configuration

Version 2 has exactly `version`, `job_database`, `inbox_base_url`,
`inbox_requestor_token`, `webhook_url`, and `sender_key`. Its webhook URL must
use HTTPS without embedded credentials or fragments, and both credential
strings must be non-empty without whitespace or control characters. A version
3 file cannot include these credential fields or fall back to version 2 when
Vault is unavailable.

To migrate an installation, confirm the two Vault entries and existing session,
then replace the three credential fields with the two Vault references and set
`version = 3`. Keep the journal and inbox origin settings. Validate Vault loading
before reconnecting Codex, then remove superseded plaintext credentials and
backups through an approved cleanup after checking recovery needs. Deployment,
Vault consumer registration, and cleanup of existing private files are separate
operator actions; changing this repository does not perform them.

## Journal and callback origin

The journal's parent directory must be owned by the current user with mode
`0700`; the server creates the database with mode `0600`. Unsupported journal
schemas are discarded and rebuilt because they belong to earlier designs.

`inbox_base_url` builds `https://<origin>/jobs/<job-id>/result`. A different
origin, hostname, path, or job ID in a callback is rejected.
