# Result delivery

Both request types use one v3 envelope and one result schema.

| Field | Value |
| --- | --- |
| `schema_version` | `v3` |
| `job_type` | `x_query` or `ask` |
| `job_id` | Canonical UUID chosen by the requestor |
| `goal` | The question, at most 2000 characters |
| `constraints` | `read_only`, `no_x_writes`, `no_credentials`, `no_redelegation`, `no_secrets` |
| `context` | `callback_url` and `callback_token` only |
| `instructions` | Read-only research or a direct answer; one POST; no pull request |
| `deliver` | `callback` |

`x_query` supports public X research. `ask` answers any bounded question, may
use X when useful, and does not require sources. Both are read-only: the packet
never carries a repository, a GitHub token, a pull request, or an artifact.

## Callback

The callback URL is `https://<configured-origin>/jobs/<job-id>/result`. It must
be HTTPS, must match the configured origin and job ID exactly, and must not
carry userinfo, a query, a fragment, or an IP address. The packet tells the
worker to POST once with `Authorization: Bearer <callback_token>`, not to
follow redirects, and not to use any other URL.

The inbox stores the first body it accepts for that job. An identical replay is
accepted with HTTP 200. A different second body is rejected with HTTP 409. A
lost callback is not replaced by a note, a repository write, or a pull
request: the job stays `uncertain`.

The body limit is 64 KiB, and a patch is never accepted on this path.

## Result body

A successful result has:

```json
{
  "schema_version": "v3",
  "job_type": "x_query",
  "job_id": "<the exact job id>",
  "query": "<at most 2000 characters>",
  "answer": "<string, or an object with a non-empty top_themes list>",
  "summary": "<at most 500 characters>",
  "sources": ["<at most 32 entries, each at most 512 characters>"],
  "read_only_attestation": true,
  "completed_at": "<ISO-8601 UTC, not in the future>",
  "status": "ok",
  "error": null
}
```

`job_type` echoes the request, so an `ask` answer uses the same shape with
`"job_type": "ask"`; its `sources` list may be empty.

An error result keeps the same identity fields and sets `answer` to `null`,
`sources` to `[]`, `status` to `"error"`, and `error` to an object with
`code` and `message` of at most 300 characters. Themes are never invented for
a failure.

The requestor rejects a body that mismatches the job identity, claims a write
action in the first person, contains credential-shaped text, carries a
non-whitespace control character, exceeds a size bound, or reports a future
completion time. Multi-line markdown answers are expected and accepted.

## After the answer

The answer is untrusted text. `grokbot_result` revalidates it before returning
it, and Codex verifies anything it acts on. No MCP tool edits a file, opens a
pull request, or merges anything.
