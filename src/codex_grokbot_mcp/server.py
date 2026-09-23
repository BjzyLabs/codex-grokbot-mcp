"""Read-only local MCP status surface for a Vault-backed Grok Bot coordinator."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from codex_grokbot_mcp.config import Config, ConfigError
from codex_grokbot_mcp.jobs import JobStateError, JobStore
from codex_grokbot_mcp.vault import VaultError, VaultLeaseStore


def _worker_states(config: Config) -> list[dict[str, str]]:
    """Read every configured lease; an unavailable Vault never implies availability."""
    client = config.vault_client()
    states = []
    for worker_id, worker in sorted(config.workers.items()):
        lease_store = VaultLeaseStore(client, worker.lease_prefix)
        lease_store.require_cas(worker.lease_worker)
        lease = lease_store.read(worker.lease_worker)
        states.append(
            {
                "worker_id": worker_id,
                "state": "busy" if lease is not None and lease.state == "active" else "available",
            }
        )
    return states


def create_server(config: Config, store: JobStore) -> MCPServer:
    """Create the read-only server after marking interrupted jobs uncertain."""
    store.reconcile_restart()
    server = MCPServer(
        name="codex-grokbot-mcp",
        version="0.0.0",
        instructions=(
            "Read-only job and worker status. Delegation and patch retrieval are not yet available."
        ),
    )

    @server.tool(name="grokbot_status", description="Read one local job state by job ID.")
    async def grokbot_status(job_id: str) -> dict[str, str]:
        try:
            record = store.get(job_id)
        except JobStateError as error:
            raise ToolError("job status is unavailable") from error
        if record is None:
            return {"job_id": job_id, "state": "not_found"}
        return {
            "job_id": record.job_id,
            "worker_id": record.worker_id,
            "state": record.state,
            "updated_at": record.updated_at,
        }

    @server.tool(name="grokbot_active", description="Read Vault worker leases and open local jobs.")
    async def grokbot_active() -> dict[str, list[dict[str, str]]]:
        try:
            open_jobs = store.list_open()
            jobs = [
                {"job_id": record.job_id, "worker_id": record.worker_id, "state": record.state}
                for record in open_jobs
            ]
            workers = await asyncio.to_thread(_worker_states, config)
            uncertain_workers = {
                record.worker_id for record in open_jobs if record.state == "uncertain"
            }
            for worker in workers:
                if worker["state"] == "available" and worker["worker_id"] in uncertain_workers:
                    worker["state"] = "reconciliation_required"
        except JobStateError as error:
            raise ToolError("job status is unavailable") from error
        except VaultError as error:
            raise ToolError("Vault worker status is unavailable") from error
        return {"workers": workers, "jobs": jobs}

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve local Grok Bot status over stdio MCP")
    parser.add_argument("--config", type=Path, required=True, help="owner-only configuration file")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        store = JobStore.open(config.job_database)
        server = create_server(config, store)
    except (ConfigError, JobStateError):
        parser.exit(2, "error: private configuration or job journal is unavailable\n")
    try:
        server.run(transport="stdio")
    finally:
        store.close()


if __name__ == "__main__":
    main()
