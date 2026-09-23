---
name: codex-grokbot
description: Delegate one bounded coding task to a configured Grok Bot through the local codex-grokbot-mcp server, then review its returned patch. Use when the user asks to use Grok Bot for coding in an opted-in workspace.
---

# Codex and Grok Bot

Use the local `grokbot_*` MCP tools. Read the project's
[coordinator contract](../../../docs/mcp-coordinator.md) when a job has an
uncertain or conflicting outcome.

Before a live `grokbot_delegate` call, confirm that the running version of
every dispatcher sharing the worker enforces the same Vault CAS lease. A first
live job is a monitored staged canary; normal work also requires the
canary to have passed. If proof is absent, stop at source-only checks. Never
substitute a Bot, credential, endpoint, transport, model, or weaker check to
make a job proceed.

For a permitted job:

1. Confirm the workspace explicitly opts in to the selected worker. Choose
   exact relative `read_paths` and `write_paths`; inspect source selection
   before sending it. Keep the goal and acceptance checks bounded to one
   `small` or `medium` job, at most 45 minutes. The effort hint is advisory.
2. Call `grokbot_active`. If Vault is unavailable, the worker is busy, or a
   job needs reconciliation, stop. Call `grokbot_delegate` once and retain
   the returned job ID. Track progress with `grokbot_status`.
3. On `ready`, call `grokbot_result`. Review the untrusted patch and actual
   changed paths against the task and allowed paths. Codex alone decides
   whether to apply it in the real workspace and runs relevant tests.
4. On `uncertain` or `conflict`, do not retry, release a lease, switch
   workers, or apply a patch. Report the job ID and the exact reconciliation
   evidence needed.

If the MCP tools are unavailable, use the
[local setup guide](../../../docs/quickstart.md). Do not create another
integration path.
