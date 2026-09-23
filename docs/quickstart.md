# Local Codex setup

This is a local stdio MCP server. Run it on the same host as Codex and the
opted-in source workspace. It requires Python 3.12+, Vault, a private control
repository for private source, and a Grok Bot webhook that implements the v2
coding packet and artifact contract.

## Before connecting

1. Provision the exact Vault AppRole, secret paths, and KV v2 lease metadata
   described in [Vault setup](vault-setup.md). Keep credential files and the
   configuration outside this public checkout. The configuration and AppRole
   files must be regular, owner-owned files with mode `0600`; the journal
   parent directory must have mode `0700`.
2. Confirm the GitHub App installation is limited to the private control
   repository and grants only the required permissions. See
   [control transport](control-transport.md).
3. Verify the **installed running revision** of every existing dispatcher
   sharing the worker uses the same Vault CAS lease key and schema. Source
   merged to Git is insufficient. The first live job is a monitored staged
   canary; normal delegation follows its acceptance.
4. Copy [the example configuration](../config.example.toml) to an owner-only
   path outside Git. Replace every synthetic endpoint, Vault path, CA path,
   repository name, and workspace path with the deployment's exact values.
   Set `enabled = true` only for a workspace that explicitly opts in to the
   named worker. Never put secret values in the TOML file.

There is no alternate secret backend, transport, or lease mode. If a required
path, permission, installed guard, or live contract is missing, stop and
record the blocker. Do not redirect a job to another Bot or endpoint.

## Install and connect

From a clean checkout of this repository, create or use its Python 3.12+
virtual environment and install the package there:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

If `.venv` already exists, use it instead of recreating it. The installation
adds the official Python MCP SDK declared by the package; it does not create
Vault or GitHub credentials.

After the live gate above is satisfied, register the stdio command in Codex
with absolute paths:

```sh
codex mcp add codex-grokbot -- /absolute/path/to/checkout/.venv/bin/python -m codex_grokbot_mcp.server --config /absolute/private/config.toml
codex mcp list
```

Codex also supports the equivalent `[mcp_servers.codex-grokbot]` entry in
its private `config.toml` with `command` and `args`; see the
[official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Keep this machine-specific entry outside the public repository. Restart the
Codex client after adding the server, then confirm it lists
`grokbot_status`, `grokbot_active`, `grokbot_delegate`, and
`grokbot_result`. A working status tool is a protocol check, not proof that
the worker webhook accepts v2 jobs.

The optional [Codex skill](../.agents/skills/codex-grokbot/SKILL.md) lives in
this repository. Codex discovers it while working in this checkout. To use it
from another repository, install the reviewed skill folder in a
[supported user skill location](https://learn.chatgpt.com/docs/build-skills),
such as `~/.agents/skills/codex-grokbot`. The skill contains no credentials.

## One bounded task

Choose one configured worker and name the exact relative source files it may
read and modify. For example, an opted-in project could submit a goal with
`read_paths = ["module.py"]`, `write_paths = ["module.py"]`, one or more
acceptance checks, and `effort_hint = "small"`. The hint is advisory; it
does not select a model or enlarge the worker's authority. Split work that
cannot fit a 45-minute job.

`grokbot_delegate` returns a job ID immediately. Use `grokbot_status` to
check progress and `grokbot_result` only when the state is `ready`. Codex
must review the patch and actual changed paths, apply an accepted patch in
the source workspace, and run its relevant tests. No MCP tool applies a patch
or merges a PR for Codex.

If a job becomes `uncertain` or `conflict`, do not resubmit it or release
its lease from local journal state. Follow the
[reconciliation boundary](mcp-coordinator.md) and compare the exact live
Vault lease and control branch before any operator action.
