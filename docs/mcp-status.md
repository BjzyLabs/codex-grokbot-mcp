# Read-only MCP status

The stdio entry point is
`python -m codex_grokbot_mcp.server --config /absolute/private/config.toml`.
The configuration file stays outside Git and is readable only by its owner. The
server opens the private SQLite journal, serves MCP through the official Python
SDK, and reconciles interrupted requests when a session starts.

`grokbot_status(job_id)` returns the job ID, request type, state, and update
time. An unknown or unusable ID returns `state: not_found`. The response
contains no prompt text, credential, callback token, or callback URL.

`grokbot_active()` returns open local requests: `queued`, `dispatching`,
`dispatched`, and `uncertain`. There is no worker state to report, because this
server holds no lease and cannot see the Bot's own queue.

`grokbot_result(job_id)` returns state only until the request is terminal. A
`ready` request is revalidated from the stored callback body; a `failed` request
reports its recorded error code and message; a `conflict` request reports its
state plus the recorded validation reason; an `uncertain` request reports only
its state.

These tools never post a request twice, never retry an uncertain webhook
outcome, and never accept a patch. See
[the coordinator contract](mcp-coordinator.md) for the dispatch path.
