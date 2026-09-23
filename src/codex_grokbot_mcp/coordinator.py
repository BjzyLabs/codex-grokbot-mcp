"""One-shot, bounded coding dispatch with durable lease and artifact decisions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from codex_grokbot_mcp.config import Config, ConfigError, WorkerConfig
from codex_grokbot_mcp.control import (
    ControlError,
    GitHubControl,
    InstallationToken,
    WebhookTransport,
    WebhookUncertain,
)
from codex_grokbot_mcp.jobs import JobStateError, JobStore, artifact_digest
from codex_grokbot_mcp.local import ContractError, DelegationContext, Workspace, validate_artifact
from codex_grokbot_mcp.protocol import EFFORT_HINTS, PacketError, build_coding_packet
from codex_grokbot_mcp.vault import LeaseRecord, VaultError, VaultLeaseStore, WorkerBusy

LOGGER = logging.getLogger(__name__)
LEASE_RENEW_MARGIN = timedelta(minutes=1)
LEASE_OWNER = "codex-grokbot-mcp"


class CoordinatorError(ValueError):
    """A local request is unsafe or its result cannot be trusted."""


class Coordinator:
    """Dispatch once, then retain uncertain work for explicit reconciliation."""

    def __init__(self, config: Config, store: JobStore, *, poll_seconds: float = 10.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self.config = config
        self.store = store
        self.poll_seconds = poll_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def delegate(
        self,
        *,
        root: Path,
        worker_id: str,
        goal: str,
        read_paths: list[str],
        write_paths: list[str],
        acceptance_checks: list[str],
        effort_hint: str,
    ) -> dict[str, str]:
        """Journal a selected snapshot and return before the worker runs."""
        if (
            not isinstance(goal, str)
            or not 0 < len(goal.strip()) <= 2000
            or "\x00" in goal
            or not isinstance(acceptance_checks, list)
            or not 1 <= len(acceptance_checks) <= 10
            or any(
                not isinstance(check, str) or not 0 < len(check.strip()) <= 300 or "\x00" in check
                for check in acceptance_checks
            )
            or effort_hint not in EFFORT_HINTS
        ):
            raise CoordinatorError("goal, acceptance checks, or effort hint are invalid")
        try:
            selected_root = Path(root).resolve(strict=True)
            worker = self.config.require_workspace(selected_root, worker_id)
            workspace = await asyncio.to_thread(Workspace.open, selected_root, opted_in=True)
            if workspace.root != selected_root:
                raise ContractError("workspace root is not canonical")
            context = await asyncio.to_thread(
                workspace.snapshot, read_paths=read_paths, write_paths=write_paths
            )
        except (ConfigError, ContractError, OSError, TypeError, ValueError) as error:
            raise CoordinatorError("workspace opt-in or selected snapshot is invalid") from error
        job_id = str(uuid4())
        short = job_id[:8]
        branch = f"grokbot/job-coding-{short}"
        artifact_path = f"artifacts/patch-{short}.json"
        try:
            self.store.create(
                job_id,
                worker_id,
                context,
                lease_owner=LEASE_OWNER,
                control_branch=branch,
                artifact_path=artifact_path,
            )
        except JobStateError as error:
            raise CoordinatorError("job could not be recorded before dispatch") from error
        task = asyncio.create_task(
            self._run_job(
                job_id,
                worker,
                context,
                branch,
                artifact_path,
                goal,
                acceptance_checks,
                effort_hint,
            ),
            name=f"grokbot-{short}",
        )
        self._tasks[job_id] = task
        task.add_done_callback(lambda _completed: self._tasks.pop(job_id, None))
        return {"job_id": job_id, "state": "queued"}

    def _uncertain(self, job_id: str) -> None:
        """Record an outcome that cannot justify retry or lease release."""
        try:
            record = self.store.get(job_id)
            if record is not None and record.state != "uncertain":
                self.store.advance(job_id, "uncertain")
        except JobStateError:
            LOGGER.error("job outcome needs manual journal reconciliation")

    async def _fail_before_post(
        self,
        job_id: str,
        worker: WorkerConfig,
        lease_store: VaultLeaseStore,
        control: GitHubControl,
        token: InstallationToken | None,
    ) -> None:
        try:
            if token is not None:
                await asyncio.to_thread(control.revoke_token, token.value)
            await asyncio.to_thread(
                lease_store.release, worker.lease_worker, owner=LEASE_OWNER, job_id=job_id
            )
            self.store.advance(job_id, "failed")
        except (ControlError, VaultError, JobStateError):
            self._uncertain(job_id)

    async def _run_job(
        self,
        job_id: str,
        worker: WorkerConfig,
        context: DelegationContext,
        branch: str,
        artifact_path: str,
        goal: str,
        acceptance_checks: list[str],
        effort_hint: str,
    ) -> None:
        try:
            await self._execute_job(
                job_id,
                worker,
                context,
                branch,
                artifact_path,
                goal,
                acceptance_checks,
                effort_hint,
            )
        except Exception:
            self._uncertain(job_id)
            LOGGER.error("job coordination stopped; manual reconciliation is required")

    async def _execute_job(
        self,
        job_id: str,
        worker: WorkerConfig,
        context: DelegationContext,
        branch: str,
        artifact_path: str,
        goal: str,
        acceptance_checks: list[str],
        effort_hint: str,
    ) -> None:
        vault = self.config.vault_client()
        lease_store = VaultLeaseStore(vault, worker.lease_prefix)
        control = GitHubControl(
            vault,
            worker.app_secret_path,
            self.config.control_repository,
            self.config.github_ca_file,
        )
        try:
            lease = await asyncio.to_thread(
                lease_store.acquire, worker.lease_worker, owner=LEASE_OWNER, job_id=job_id
            )
        except WorkerBusy:
            self.store.advance(job_id, "failed")
            return
        except VaultError:
            self._uncertain(job_id)
            return
        try:
            self.store.advance(job_id, "lease_held")
        except JobStateError:
            self._uncertain(job_id)
            return
        token: InstallationToken | None = None
        try:
            secret = await asyncio.to_thread(vault.read_secret, worker.webhook_secret_path)
            url, sender_key = secret.get("webhook_url"), secret.get("sender_key")
            if not isinstance(url, str) or not isinstance(sender_key, str):
                raise ControlError("webhook secret is incomplete")
            webhook = WebhookTransport(url, sender_key, self.config.webhook_ca_file)
            token = await asyncio.to_thread(control.mint_worker_token)
            packet = build_coding_packet(
                job_id=job_id,
                goal=goal,
                context=context,
                control_repo=self.config.control_repository,
                control_branch=branch,
                artifact_path=artifact_path,
                token=token,
                acceptance_checks=acceptance_checks,
                effort_hint=effort_hint,
            )
            self.store.record_token_expiry(job_id, token.expires_at)
            self.store.advance(job_id, "dispatching")
        except (VaultError, ControlError, PacketError, JobStateError):
            await self._fail_before_post(job_id, worker, lease_store, control, token)
            return
        try:
            await asyncio.to_thread(webhook.dispatch, packet)
        except (WebhookUncertain, ControlError):
            self._uncertain(job_id)
            return
        try:
            self.store.advance(job_id, "dispatched")
        except JobStateError:
            self._uncertain(job_id)
            return
        await self._poll_job(
            job_id, worker, context, branch, artifact_path, lease, lease_store, control, token
        )

    async def _poll_job(
        self,
        job_id: str,
        worker: WorkerConfig,
        context: DelegationContext,
        branch: str,
        artifact_path: str,
        lease: LeaseRecord,
        lease_store: VaultLeaseStore,
        control: GitHubControl,
        token: InstallationToken,
    ) -> None:
        while True:
            now = datetime.now(UTC)
            if lease.deadline_at is None or now >= lease.deadline_at:
                self._uncertain(job_id)
                try:
                    await asyncio.to_thread(control.revoke_token, token.value)
                except ControlError:
                    LOGGER.error("timed-out worker token revocation needs reconciliation")
                return
            try:
                if lease.expires_at is None or now >= lease.expires_at - LEASE_RENEW_MARGIN:
                    lease = await asyncio.to_thread(
                        lease_store.renew, worker.lease_worker, owner=LEASE_OWNER, job_id=job_id
                    )
                pr = await asyncio.to_thread(control.find_artifact_pr, token.value, branch)
            except (VaultError, ControlError):
                self._uncertain(job_id)
                return
            if pr is None:
                await asyncio.sleep(self.poll_seconds)
                continue
            try:
                artifact = await asyncio.to_thread(
                    control.read_artifact, token.value, artifact_path, pr.head_sha
                )
                if artifact.get("schema_version") != "v2":
                    raise ContractError("returned artifact version does not match the request")
                self.store.advance(job_id, "artifact_received")
                await asyncio.to_thread(validate_artifact, context, job_id, artifact)
                summary = artifact.get("summary")
                if not isinstance(summary, str) or len(summary) > 2000:
                    raise ContractError("artifact summary is invalid")
                digest = artifact_digest(artifact)
                self.store.record_artifact_identity(job_id, pr.head_sha, digest)
                self.store.advance(job_id, "validated")
            except ContractError:
                try:
                    await asyncio.to_thread(control.revoke_token, token.value)
                    self.store.advance(job_id, "conflict")
                except (ControlError, JobStateError):
                    self._uncertain(job_id)
                return
            except (ControlError, JobStateError):
                self._uncertain(job_id)
                return
            try:
                await asyncio.to_thread(control.revoke_token, token.value)
                await asyncio.to_thread(
                    lease_store.release, worker.lease_worker, owner=LEASE_OWNER, job_id=job_id
                )
                self.store.advance(job_id, "ready")
            except (ControlError, VaultError, JobStateError):
                self._uncertain(job_id)
            return

    async def result(self, job_id: str) -> dict:
        """Return only a freshly checked patch from the pinned immutable commit."""
        try:
            record = self.store.get(job_id)
        except JobStateError as error:
            raise CoordinatorError("job status is unavailable") from error
        if record is None:
            return {"job_id": job_id, "state": "not_found"}
        if record.state != "ready":
            return {"job_id": job_id, "state": record.state}
        try:
            worker = self.config.require_workspace(record.root, record.worker_id)
            workspace = await asyncio.to_thread(Workspace.open, record.root, opted_in=True)
            context = await asyncio.to_thread(
                workspace.snapshot,
                read_paths=list(record.read_paths),
                write_paths=list(record.write_paths),
            )
            if (
                context.root != record.root
                or context.target_repo != record.target_repo
                or context.head != record.head
                or context.branch != record.branch
                or context.snapshot_digest != record.snapshot_digest
            ):
                self.store.advance(job_id, "conflict")
                return {"job_id": job_id, "state": "conflict"}
            vault = self.config.vault_client()
            control = GitHubControl(
                vault,
                worker.app_secret_path,
                self.config.control_repository,
                self.config.github_ca_file,
            )
            token = await asyncio.to_thread(control.mint_worker_token)
        except (ConfigError, ContractError) as error:
            self.store.advance(job_id, "conflict")
            raise CoordinatorError("pinned result is unavailable") from error
        except (ControlError, VaultError, JobStateError) as error:
            raise CoordinatorError("pinned result is unavailable") from error
        try:
            artifact = await asyncio.to_thread(
                control.read_artifact,
                token.value,
                record.artifact_path,
                record.artifact_head_sha,
            )
            if (
                artifact.get("schema_version") != "v2"
                or artifact_digest(artifact) != record.artifact_sha256
            ):
                raise ContractError("pinned artifact changed")
            validated = await asyncio.to_thread(validate_artifact, context, job_id, artifact)
            summary = artifact.get("summary")
            if not isinstance(summary, str) or len(summary) > 2000:
                raise ContractError("artifact summary is invalid")
        except ControlError as error:
            raise CoordinatorError("pinned result is unavailable") from error
        except (ContractError, JobStateError) as error:
            self.store.advance(job_id, "conflict")
            raise CoordinatorError("pinned result failed validation") from error
        finally:
            try:
                await asyncio.to_thread(control.revoke_token, token.value)
            except ControlError as error:
                raise CoordinatorError("result token revocation is unconfirmed") from error
        return {
            "job_id": job_id,
            "state": "ready",
            "patch": validated.patch,
            "changed_paths": list(validated.changed_paths),
            "summary": summary,
        }
