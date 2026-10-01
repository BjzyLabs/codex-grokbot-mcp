# Local Codex setup

This is a local stdio MCP server. Run it on the same host as Codex, with a
reachable Grok Bot webhook and the callback inbox origin that Grok Bot can
reach. Python 3.12+ is required.

## Before connecting

1. Confirm the Grok Bot routine accepts a v3 `x_query` or `ask` packet and
   answers by callback. The payload contract is in
   [result delivery](result-delivery.md).
2. Confirm the callback inbox is reachable from the Bot and that you hold its
   requestor credential.
3. Copy [the example configuration](../config.example.toml) to an owner-only
   regular file outside Git, replace every synthetic value, and `chmod 0600`
   it. The rules are in [configuration](configuration.md). Never put the
   configuration file in the repository.
4. Confirm the journal path's parent directory is private (`0700`). The server
   creates the `0600` database file on first run.

## Install and connect

From a clean checkout of this repository, create or use its Python 3.12+
virtual environment and install the package there:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

If `.venv` already exists, use it instead of recreating it. The installation
adds only the official Python MCP SDK declared by the package.

Register the stdio command in Codex with absolute paths:

```sh
codex mcp add codex-grokbot -- \
  /absolute/path/to/checkout/.venv/bin/python -m codex_grokbot_mcp.server \
  --config /absolute/private/config.toml
codex mcp list
```

Codex also supports the equivalent `[mcp_servers.codex-grokbot]` entry in its
private `config.toml` with `command` and `args`; see the
[official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Keep this machine-specific entry outside the public repository. Restart the
Codex client after adding the server and confirm it lists
`grokbot_x_query`, `grokbot_ask`, `grokbot_status`, `grokbot_result`, and
`grokbot_active`.

The optional [Codex skill](../.agents/skills/codex-grokbot/SKILL.md) lives in
this repository and contains no credentials.

## First request

Call `grokbot_ask` with one bounded question, or `grokbot_x_query` for public X
research. Both return a job ID immediately. Poll `grokbot_status` and call
`grokbot_result` once the state is `ready`.

`grokbot_result` re-fetches the stored callback, compares its digest with the
one recorded at completion, and validates the body again before returning the
summary, answer, and sources. It never applies anything and never edits a
repository.

If a job becomes `uncertain` or `conflict`, do not resubmit it. An
`uncertain` job may already have been delivered; a `conflict` job returned a
body the requestor will not accept. Report the job ID and follow the
[reconciliation boundary](mcp-coordinator.md).
