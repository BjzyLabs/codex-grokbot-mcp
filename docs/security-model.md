# Security model

The local MCP process is the trusted requestor. Version 3 reads the X Bot
webhook URL, webhook sender key, and callback inbox requestor token from Vault
into memory at startup. Its owner-only config contains only settings and Vault
references. Version 2 retains credentials in a private file for compatibility.
There is no repository credential or shared lease. Vault authentication uses
the existing CLI session; startup rejects root and expired sessions, requires
verified TLS, and fails closed on missing fields or failed reads.

A request carries only a job ID, a question, read-only constraints, and one
callback URL with one per-job token. The requestor never sends local paths,
file contents, repository identity, or a token that grants access to any
system. The callback token is stored in the inbox as a hash, and the answer is
treated as attacker-controlled text.

Uncertainty is a state, not a trigger. A webhook POST that cannot be confirmed
leaves the job `uncertain` and is never retried automatically, because the Bot
may already be answering. A callback that fails validation leaves the job
`conflict`. Jobs are capped at 45 minutes. Durable non-secret state survives a
stdio restart so a `dispatched` request can resume polling.

## Threats and boundaries

| Threat | Control | Remaining operator responsibility |
| --- | --- | --- |
| The requestor is asked to do something it should not | Fixed tool surface: two read-only request types, status, result, and active | Keep the configuration file owner-only and the webhook pointed at one Bot |
| A credential leaks through a packet or log | The packet carries only the per-job callback token; the sender key appears only in the Authorization header; the configuration excludes secrets from `repr` | Keep the configuration outside Git and rotate the two credentials if exposed |
| A duplicate or replayed callback corrupts an answer | One stored body per job; identical replay accepted, different body rejected, digest recorded at completion | Investigate a `conflict` instead of resubmitting the job |
| A timeout is mistaken for failure | `uncertain` is durable and never auto-retried | Check the Bot's own run history and the inbox before acting |
| Answer text is used as instructions | Answers are validated, bounded, and returned as data; the requestor never applies them | Treat any answer as untrusted when acting on it |
| The local process stops mid-request | Private SQLite journal keeps state, digest, and timestamps; `dispatched` requests resume | Nothing to release; a `queued` or `dispatching` job becomes `uncertain` and needs a human decision |

The Grok Bot provider receives the question and the callback URL. It is inside
the trust boundary for that question, and for nothing else.
