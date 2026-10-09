# Beads task tracking

Use Beads 1.3 or later with its embedded Dolt backend. Runtime state under
`.beads/` is ignored by Git; task history is published separately to the
repository's `refs/dolt/data` ref through `bd dolt push`. Do not edit JSONL
exports, commit the database, or force-push task history.

## Existing and new checkouts

For a new checkout, initialize from an approved task-history remote:

```sh
bd init --skip-agents --skip-hooks --non-interactive
bd list --all
```

Before changing tasks, use `bd where` to confirm the resolved database. Pull
shared changes with `bd dolt pull`, use `bd create` / `bd update` / `bd close`
for task state, and publish with `bd dolt push` after reviewing the payload.
Install standard Beads Git hooks separately if the checkout lacks them.
Public task-history publication requires review for private metadata as well
as credential and identifier scanning.

## Legacy checkout recovery

The previous tracked `.beads/issues.jsonl` used the removed `no-db` backend.
The local migration preserved 21 legacy issues and 16 comments, verified their
identity, title, description, status, priority, type, comment text, author,
and timestamp. Legacy comment IDs were only unique within each issue, so a
separate import copy omitted comment IDs and let Dolt allocate them globally.
Dolt represents timestamps to whole seconds; the old Git export preserves the
original subsecond timestamps.

The original files remain recoverable from Git history before this migration.
If an existing checkout still has the old runtime directory, back it up outside
Git before initialization. Adopt published Dolt history after publication has
been approved and verified. Confirm all legacy issue IDs and comments are
present before retiring the backup. Do not reinitialize an existing Dolt
database or discard remote history.

Migration acceptance checks:

- Every original issue ID is present once, with its issue content preserved.
- All 16 comment texts, authors, and timestamps match at whole-second precision.
- After approved publication, a fresh checkout reads the migrated issues and
  new implementation task from the remote task-history ref.
- No runtime database, metadata, backup, or private identifier is staged.

Keep private configuration, credential values, endpoints, and actual Vault
paths out of task titles, descriptions, comments, and exported task records.
