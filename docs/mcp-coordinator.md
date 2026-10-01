# MCP coordinator contract

One request becomes one webhook POST plus one callback. The coordinator holds
no lease and no repository authority, and it never reads a file from the local
workspace.

## Dispatch

`grokbot_x_query` and `grokbot_ask` validate the prompt, journal a `queued`
request with a 45-minute deadline, and return the job ID before the webhook
call finishes.

The background task then, in order:

1. Registers one inbox expectation for `/jobs/<job-id>/result` using the
   SHA-256 hash of a fresh 32-byte callback token.
2. Builds the v3 packet in memory with that token and the configured
   `inbox_base_url` origin.
3. Records `dispatching` and posts the packet once.
4. Records `dispatched` and polls the inbox every 10 seconds until the
   deadline.

The prompt itself is never journaled; it exists only in the packet.

## Outcomes

| Observation | State |
| --- | --- |
| Valid `status: "ok"` callback | `ready` |
| Valid `status: "error"` callback | `failed`, with the error code and message |
| Stored body that fails validation | `conflict`, with the validation reason recorded |
| Ambiguous webhook outcome | `uncertain` |
| Ambiguous inbox failure or deadline with no callback | `uncertain` |
| Failure proven to precede the POST | `failed` |

An ambiguous POST is never retried and never compensated. `uncertain` is the
state for "the Bot may already have answered": inspect the job, the Bot's run
history, and the inbox before deciding anything. A `conflict` means the Bot
answered with something this requestor will not accept, so the body is kept
and the job stops.

## Restart behaviour

At the start of a session the coordinator reconciles the journal:

- `dispatched` jobs resume polling with their original deadline and callback
  expectation, because the POST already succeeded and the answer may still
  arrive;
- `queued` and `dispatching` jobs become `uncertain`, because the process may
  have been stopped at any point around the POST.

Nothing is re-posted. A resumed job that reaches the deadline becomes
`uncertain` with no cleanup step to perform, because there is no lease to
release.

## Concurrency

Two requests may be in flight at once. Correctness comes from the job ID and
the inbox contract, not from mutual exclusion: each job has its own callback
path, its own token, and at most one stored body, with an identical replay
accepted and a different second body rejected. This is safe because both
request types are read-only.

## Result

`grokbot_result` returns state only until the job is `ready`. For a `ready` job
it re-fetches the stored body, requires the recorded callback digest, validates
the body again, and only then returns the summary, answer, and sources. A
changed or invalid body moves the job to `conflict` and returns an MCP tool
error. A `failed` job reports the recorded error code and message; a `conflict`
job reports the recorded validation reason alongside its state.
