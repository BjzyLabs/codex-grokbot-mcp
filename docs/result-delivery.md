# Result delivery

v3 jobs choose one result authority.

| Job | Default `deliver` | What comes back |
|---|---|---|
| `x_query` | `callback` | One HTTPS POST. No control-repository token and no pull request. |
| `coding` | `github_pr` | A draft pull request whose patch is pinned and revalidated. |

An omitted `deliver` uses that table. A value that disagrees with the job type is rejected. v2 coding packets stay valid when no result inbox is configured. v1 answer artifacts remain readable. Neither is reused as the v3 default.

## Callback

The packet carries `callback_url` and one `callback_token`. The worker sends exactly `Authorization: Bearer <callback_token>`. There is no header map. The URL is `https://<configured-origin>/jobs/<job-id>/result`. It must be HTTPS, must match the configured origin and job id, and must not carry userinfo, a query, a fragment, or an IP address.

The worker posts once and does not follow redirects. The inbox stores that body until the requestor reads it. An identical replay is accepted. A different second body is rejected. A lost callback does not fall back to a GitHub note; the job stays `uncertain` and the lease stays held.

A successful body has `status: "ok"`, a real answer, and `read_only_attestation: true`. An error body has `status: "error"`, `answer: null`, `sources: []`, and an error object. Themes are not invented for a failure. Text bounds match the read-only answer contract: query ≤ 2000, summary ≤ 500, string answer ≤ 4000, structured answer ≤ 20000 with a non-empty `top_themes` list, and at most 32 sources.

The callback body limit is 64 KiB. A patch is never accepted on this path.

## Worker diagnostic callback

`worker_diagnostic` is a separate v3 request used to ask Chief of Staff about
one existing coding job. The configured target worker maps to a same-account
Chief of Staff worker and to `coder` or `devcoder`; the mapping is explicit in
private config. The packet contains a fixed read-only question and callback
credentials only. Chief of Staff must not forward the callback token to the
target Bot.

The MCP registers the normal `/jobs/<diagnostic-id>/result` one-use callback,
then posts the packet to the configured Chief of Staff webhook from Vault. The
MCP never acquires a coding lease or mints a GitHub token for this operation.
Its requestor callback client reads `requestor_token` from the configured Vault
secret and uses the configured HTTPS origin, the webhook CA bundle, and no
redirects. The Bot may wait up to 120 seconds before sending its callback, so
the MCP keeps the callback registration open for a further 90 seconds.

The exact response fields are `schema_version`, `job_type`, `job_id`,
`target_job_id`, `target_bot`, `status`, `reply`, `completed_at`, and
`read_only_attestation`. Status is `replied`, `no_reply`, or
`delivery_failed`; reply is at most 2000 safe characters or null for an empty
status. Completion time must be UTC `Z`. The callback is stored by digest and
the validated reply is kept in the private SQLite journal. `no_reply` means
Chief of Staff explicitly reported that the target Bot did not respond. A
missing callback is `uncertain`; it does not establish whether the target Bot
was contacted. An interrupted diagnostic becomes `uncertain` on restart and is
not automatically resent.

A diagnostic reply is evidence from Chief of Staff. It is not authoritative
proof that Cursor is idle, does not confirm an artifact, and cannot release a
lease, retry work, or mark a coding job complete. Check the coding job's
artifact and lease independently before any recovery decision.

## Coding status ping

When a result inbox is configured, a coding packet may also include `status_callback_url` and `status_callback_token`. That POST is a hint to poll the draft pull request. `ready` does not accept the code. `blocked` or `error` can finish the job only when no artifact pull request exists. If a pull request also exists, the validated artifact wins. A status body that contains `patch`, `diff`, or `github_token`, or names another repository, is rejected.

## Who applies the change

The MCP never applies a patch and never treats a callback body as a diff. Codex reviews, applies, and tests a coding result. An `x_query` result is text, not a workspace edit.

Workers declare `job_types`. Omitted types stay `coding` only. An `x_query` sent to a coding worker fails closed. There is no automatic failover.

The inbox process binds to loopback. Putting it on a public HTTPS origin is an operator deployment step and is not part of the stdio server. Without that origin, callback jobs fail closed and coding continues on the v2 control-repository path.
