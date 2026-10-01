from __future__ import annotations

from collections.abc import Mapping

from app.bot.commands import (
    CommandContext,
    ProviderModels,
    StatusSnapshot,
    SubmitResult,
    SubmitStatus,
    UsageSnapshot,
)
from app.config.models import AppConfig
from app.db.provider_models import ProviderModelRepository
from app.db.quota import QuotaSnapshotRepository
from app.db.sessions import AmbiguousReference, SessionRecord, SessionRepository, SessionStatus
from app.db.tasks import TaskRepository
from app.db.usage import UsageRepository
from app.orchestrator.orchestrator import CancelResult, sanitize_prompt
from app.quota.estimator import estimate_quota
from app.quota.normalize import ESTIMATE_SOURCE_PREFIX
from app.router.policy import ModelRoutingStatus, model_listing
from app.runtime.daemon import AgentController, TaskSubmission
from app.timeutil import utcnow


class ControllerBackend:
    """BotBackend over the controller and orchestrator."""

    def __init__(
        self,
        controller: AgentController,
        config: AppConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._controller = controller
        self._config = config
        self._environ = environ

    def _conn(self):  # noqa: ANN202
        conn = self._controller.conn
        if conn is None:
            raise RuntimeError("controller not started")
        return conn

    def submit_code(
        self, *, prompt: str, project: str | None, session_ref: str | None, ctx: CommandContext
    ) -> SubmitResult:
        clean = sanitize_prompt(prompt)
        if clean is None:
            return SubmitResult(SubmitStatus.INVALID_PROMPT)
        project_id = project or "default"
        if self._config.opencode.project_directory(project_id) is None:
            return SubmitResult(SubmitStatus.UNKNOWN_PROJECT)
        session_id: str | None = None
        if session_ref is not None:
            try:
                session = SessionRepository(self._conn()).find_by_ref(session_ref)
            except AmbiguousReference:
                return SubmitResult(SubmitStatus.AMBIGUOUS_SESSION)
            if session is None:
                return SubmitResult(SubmitStatus.UNKNOWN_SESSION)
            if session.status != SessionStatus.OPEN:
                return SubmitResult(SubmitStatus.SESSION_CLOSED)
            session_id = session.id
            project_id = session.project_id
        record = self._controller.enqueue_task(
            TaskSubmission(
                project_id=project_id,
                prompt=clean,
                discord_user_id=str(ctx.user_id),
                discord_guild_id=None if ctx.guild_id is None else str(ctx.guild_id),
                discord_channel_id=None if ctx.channel_id is None else str(ctx.channel_id),
                session_id=session_id,
            )
        )
        if record is None:
            return SubmitResult(SubmitStatus.QUEUE_FULL)
        depth = self._controller.metrics()["task_queue_depth"]
        return SubmitResult(SubmitStatus.QUEUED, record, int(depth or 0))

    def cancel(self, task_ref: str) -> tuple[CancelResult | None, str]:
        try:
            record = TaskRepository(self._conn()).find_by_ref(task_ref)
        except AmbiguousReference:
            return None, "ambiguous id"
        if record is None:
            return None, "Unknown task."
        return self._controller.cancel_task(record.id), ""

    async def status(self) -> StatusSnapshot:
        orch = self._controller.orchestrator
        if orch is None:
            raise RuntimeError("orchestrator not configured")
        health = await orch.executor.health_check()
        depth = self._controller.metrics()["task_queue_depth"]
        recent = TaskRepository(self._conn()).list_recent(5)
        return StatusSnapshot(health, orch.running, int(depth or 0), tuple(recent))

    def usage(self) -> UsageSnapshot:
        orch = self._controller.orchestrator
        if orch is None:
            raise RuntimeError("orchestrator not configured")
        conn = self._conn()
        now = utcnow()
        exact = [
            r
            for r in QuotaSnapshotRepository(conn).latest(now)
            if not r.source.startswith(ESTIMATE_SOURCE_PREFIX)
        ]
        usage_repo = UsageRepository(conn)
        quota = exact + estimate_quota(self._config, usage_repo, now, exact=exact)
        return UsageSnapshot(
            quota=tuple(quota),
            cooldowns=orch.cooldowns.active(now),
            today=tuple(usage_repo.daily_totals(now)),
            now=now,
        )

    def models(self) -> list[ProviderModels]:
        conn = self._conn()
        repo = ProviderModelRepository(conn)
        routing = self._config.routing
        out: list[ProviderModels] = []
        for pid in routing.provider_order:
            provider = routing.policies[pid].provider
            listings = tuple(
                m
                for m in model_listing(self._config, provider, repo.list_model_ids(provider))
                if m.policy_id == pid or m.status == ModelRoutingStatus.DISCOVERED_ONLY
            )
            caps = {c.model: c for c in repo.allowed_capabilities(self._config, provider)}
            out.append(ProviderModels(pid, provider, listings, caps))
        return out

    def sessions(self, limit: int = 10) -> list[SessionRecord]:
        return SessionRepository(self._conn()).list_recent(limit)
