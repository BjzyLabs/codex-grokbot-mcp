# Local coding contract

The local contract is the first implementation boundary. It performs no Vault, GitHub, or Grok Bot calls and does not apply changes to the user's workspace.

`Workspace.open` requires an explicit opt-in decision from the caller. The eventual configuration loader must derive that decision from a per-workspace setting; a caller must never assume that a private workspace is authorized. The workspace must have a GitHub origin. Snapshots use repository-relative UTF-8 paths and include only selected files. Existing write paths must also be readable. New write paths can be declared without a source snapshot. Symlinks, binary files, oversized files, and sensitive or escaping paths fail closed.

The snapshot digest covers the repository identity, HEAD, selected paths, and file hashes. A v2 coding artifact must echo that digest and `workspace_head`. A v1 artifact uses `base_sha` and remains readable for compatibility. Both versions require exact job and target repository identity.

Patch validation asks Git to parse the diff. The artifact's declared changed paths must equal Git's actual changed paths, and every actual path must be in `write_paths`. Renames, copies, binary patches, unsupported mode changes, and path traversal are rejected. The validator rechecks the real workspace for drift, then checks and applies the patch only in a disposable worktree reconstructed from the exact delegated snapshots. It returns the untrusted patch for Codex review; Codex owns any application to the real workspace and its tests.

The local checks do not prove webhook behavior, token scope, Vault leases, restart recovery, or live worker output. Those are separately tracked milestones.
