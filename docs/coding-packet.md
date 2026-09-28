# Bounded coding packet

The packet builder emits a v2 coding job over the existing first-party webhook.
It retains the proven v1 routing fields: `control_repo`, `head_ref`,
`github_token`, `target_repo`, `base_sha`, `allowed_paths`, and `patch_path`.
The GitHub token is scoped to the private control repository by the transport;
it never grants access to the target repository. The sender key stays in the
HTTPS authorization header and is not part of the packet.

## Artifact version

Request-envelope and artifact versions are independent. The v3 delivery
envelope keeps the coding artifact at schema version `v2`; every live coding
artifact must set `schema_version` to `v2` and `job_type` to `coding`. The live
coordinator rejects a v1 artifact. If the worker cannot provide the v2 fields,
it must report a blocker instead of downgrading the artifact.

The artifact must echo `job_id`, `target_repo`, `base_sha`, `workspace_head`,
and `snapshot_digest` from the request. It also contains the declared paths,
summary, unified diff, and completion timestamp:

```json
{
  "schema_version": "v2",
  "job_type": "coding",
  "job_id": "<job_id>",
  "target_repo": "<context.target_repo>",
  "base_sha": "<context.base_sha>",
  "workspace_head": "<context.workspace_head>",
  "snapshot_digest": "<context.snapshot_digest>",
  "declared_changed_paths": ["<path>"],
  "summary": "<summary>",
  "patch": "<unified diff>",
  "completed_at": "<UTC timestamp>"
}
```

The v2 fields bind the exact Git HEAD, snapshot digest, separate read and write
paths, and only the selected source snapshots. The packet includes no local
workspace path. The worker is instructed to return one patch artifact on the
job's control branch and open a draft PR. It must report blockers without
substituting credentials, transports, hosts, models, or weaker checks. Codex
validates every returned artifact and decides whether to apply the patch.

A goal and one to ten acceptance checks tell the Bot what work is expected.
The `effort_hint` is `small` or `medium` and is advisory. Larger work must be
split before submission. The 45-minute maximum, path limits, token scope, and
review checks do not change with the hint. Grok Bot has no exposed model picker
for this integration; Cursor manages its model selection.

The v1-compatible branch and artifact names use the first eight job-ID hex
characters. The private job journal enforces unique branch and artifact paths,
so a prefix collision fails before dispatch. The v2 webhook and artifact
behavior still require a staged live canary after both dispatchers run the
shared Vault lease guard. Local packet tests do not establish that live result.

v3 keeps this coding route when `deliver` is `github_pr`. Read-only `x_query`
jobs use a callback instead of a control-repository artifact. See
[result delivery](result-delivery.md).
