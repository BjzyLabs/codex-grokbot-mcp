# v3 result delivery plan

**Status:** Implemented on `develop`. Not a `main` release.  
**Boundary:** This repository's packet builder, validators, loopback inbox, and Mac stdio tools. No live webhook. No promotion to `main`.

## Decision

Read-only answers return as one HTTPS callback. Coding changes return as a pinned draft pull request. A status ping may say the pull request is ready to poll; it cannot carry the patch. Callback failure does not fall back to a GitHub note.

Day 2 conversation, not built here: the operator's existing chat room stays the place people talk. The worker is not invited into that room. The webhook is not a chat room.

## Contract

New packets use `schema_version` `v3`.

- `x_query` defaults to `deliver: callback`. Context is `callback_url` and `callback_token` only.
- `coding` defaults to `deliver: github_pr`. The v2 control-repository fields stay. Optional status fields are `status_callback_url` and `status_callback_token`.
- A deliver value that disagrees with the job type is rejected.
- Workers declare `job_types`. The default is coding only. There is no automatic failover.
- Vault remains the only secret store. Per-job callback tokens are hashed at rest and are not inventoried one by one. A new long-lived inbox credential, when one is minted, gets lifecycle fields and the token inventory mirror.

The inbox binds to loopback in this repository. Publishing it on an approved HTTPS origin is a later deployment. Until that origin exists, callback jobs fail closed and coding stays on the v2 path.

## Handoff after this merge

These prompts are for the operator to paste. They do not authorize a live job.

### Worker inbox

```text
The public contract for codex-grokbot-mcp v3 is now on develop. Update the Outside agent inbox prompt to enforce it. Reply with any line you cannot enforce. Do not invent a fallback. Do not dispatch or wait for a live job.

Your role:
- You are the worker. Requestors send one webhook packet. You return one result.
- x_query uses deliver=callback. POST the answer once. Do not open a PR, do not write a control repo, and do not look for a GitHub token.
- coding uses deliver=github_pr. The draft PR is the result. An optional status POST is a ping, never the patch.
- Do not join a chat room for this contract. The operator's dispatcher relays conversation. The webhook is not a chat room.

Defaults when deliver is omitted: x_query -> callback, coding -> github_pr. If deliver disagrees with the job type, refuse the packet.

x_query context is only callback_url and callback_token. POST once with exactly:
  Authorization: Bearer <callback_token>
Do not follow redirects. Use no other URL.

Success body: schema_version v3, job_type x_query, job_id, query, answer, summary, sources, read_only_attestation true, completed_at ISO-8601 UTC, status "ok", error null.

Error body: answer null, sources [], status "error", error {code, message}, read_only_attestation true. Do not invent themes.

coding keeps the control-repo draft PR. Optional status body fields are schema_version, job_type "coding", job_id, status "ready|blocked|error", pr_url or null, summary, completed_at. Do not include patch, diff, or github_token.

When the inbox prompt is loaded, reply "v3 inbox prompt loaded" and list any rule you changed.
```

### Dispatcher

```text
codex-grokbot-mcp develop now contains the v3 job contract (docs/result-delivery.md and the packet builder). Read that contract and adopt it in the dispatcher. This message does not deploy anything and does not authorize a live job.

Your role:
- You are a requestor, the same kind of requestor as the Mac stdio tools. The worker does not receive this behavior from the MCP install.
- The Mac path can speak v3 when its owner config has an inbox. The current read-only path still writes a GitHub answer artifact until the dispatcher changes.
- Find the git repository that owns that dispatcher before editing. A working tree with no git directory is not the source of truth. Branch from its integration branch, use tests first, track the work, and stop before the release branch.
- Vault remains the only secret store. No tokens in chat, git, issue trackers, or automation extra variables.

What to implement, and nowhere else yet:
- x_query packets are schema v3, deliver callback. Send callback_url and callback_token only. Do not fall back to a GitHub answer PR. Leave a missing callback uncertain and keep the shared lease.
- coding packets stay deliver github_pr. The pinned draft-PR artifact is still the only ready result. A status callback cannot accept a patch.
- Validate callback bodies before showing them. status=ok needs a real answer. status=error needs answer null and an error object.
- Claim the same shared worker lease before dispatch. No automatic failover.

Do not deploy the inbox and do not send a live webhook until the operator explicitly starts that follow-up. Reply with the git repo and branch you would change, and stop there.
```

## Verification

Local tests cover the packet rules, the inbox store, and the coordinator branches. They do not prove a live webhook, a public inbox origin, or a worker prompt change. Those stay gated. The earlier single-file coding canary remains a `develop` milestone. Multi-file, drift, rejection, restart, and full workstation acceptance still gate `main`.
