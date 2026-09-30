# Local configuration

One flat, owner-only TOML file configures the requestor. It is the only
credential store in this project: no secret backend, CA bundle, workspace
allowlist, or worker table is read.

Copy `config.example.toml` outside the repository, replace every value, and set
the file mode to `0600`. The loader rejects any other mode, a file owned by
another user, a symlink, a missing key, an unknown key, and any nested table.

| Key | Rule |
| --- | --- |
| `version` | Integer `2`. Any other value is unsupported. |
| `job_database` | Absolute path to the private SQLite journal. |
| `inbox_base_url` | Canonical HTTPS origin: no path, query, fragment, userinfo, or IP address. |
| `inbox_requestor_token` | Non-empty, no whitespace or control characters. |
| `webhook_url` | HTTPS URL with a host, no userinfo and no fragment. |
| `sender_key` | Non-empty, no whitespace or control characters. |

The two credentials are held as attributes that are excluded from `repr`, and
no error message repeats a value. A failing configuration stops the server
before any request is journaled or posted.

The journal path is created when absent: the parent directory must already be
owned by the current user with mode `0700`, and the database file is created
with mode `0600`. Unsupported journal schemas are discarded and rebuilt,
because the earlier canary history belongs to a design this server no longer
implements.

`inbox_base_url` is the origin the per-job callback URL is built from:
`https://<origin>/jobs/<job-id>/result`. A different origin, hostname, path, or
job ID in a callback is rejected.
