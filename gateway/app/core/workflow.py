# -*- coding: utf-8 -*-
"""Persistent workflow shell.

The target architecture asks for a *durable* execution model with checkpoints,
behind an interface that matches Temporal so the local implementation can be
swapped for a real Temporal server later without touching call sites.

What is implemented here:

* Every run is a row in ``workflow_runs``; after each completed step a
  ``workflow_checkpoints`` row records the state, so a crashed process can
  resume instead of restarting the conversation turn.
* A run can enter ``waiting`` — that is how the human-approval branch parks a
  turn until a reviewer decides. ``signal()`` resumes it.
* The public surface is deliberately the Temporal subset:
  ``start`` / ``signal`` / ``query`` / ``get_result`` / ``cancel``.

What is *not* implemented: distributed workers, timers, and versioning. A run
is advanced by whichever process holds the request, and a background sweeper
(``scripts/workflow_sweeper.py``) resumes parked runs. This is honest for a
single-node deployment and is exactly the part Temporal would replace.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WorkflowCheckpoint, WorkflowRun, now
from .observability import log_event

logger = logging.getLogger('gateway.workflow')

RUN_TRANSITIONS: dict[str, set[str]] = {
    'running': {'waiting', 'completed', 'failed', 'cancelled'},
    'waiting': {'running', 'completed', 'failed', 'cancelled'},
    'completed': set(),
    'failed': set(),
    'cancelled': set(),
}


class WorkflowError(Exception):
    pass


@dataclass
class StepContext:
    run: WorkflowRun
    db: Session
    services: dict[str, Any] = field(default_factory=dict)
    state: dict = field(default_factory=dict)

    def save(self) -> None:
        """Persist state into the run row (call before returning an outcome)."""
        self.run.state = dict(self.state)


@dataclass
class StepOutcome:
    """What a step wants to happen next."""

    kind: str = 'continue'          # continue | wait | complete | fail
    detail: str = ''
    patch: dict = field(default_factory=dict)

    @classmethod
    def proceed(cls, **patch) -> 'StepOutcome':
        return cls('continue', patch=patch)

    @classmethod
    def wait(cls, reason: str, **patch) -> 'StepOutcome':
        return cls('wait', detail=reason, patch=patch)

    @classmethod
    def done(cls, **patch) -> 'StepOutcome':
        return cls('complete', patch=patch)

    @classmethod
    def fail(cls, reason: str, **patch) -> 'StepOutcome':
        return cls('fail', detail=reason, patch=patch)


StepHandler = Callable[[StepContext], Awaitable[StepOutcome]]

WORKFLOWS: dict[str, list[str]] = {}
STEP_HANDLERS: dict[tuple[str, str], StepHandler] = {}


def define_workflow(workflow_type: str, steps: list[str]) -> None:
    WORKFLOWS[workflow_type] = list(steps)


def step(workflow_type: str, step_name: str) -> Callable[[StepHandler], StepHandler]:
    def decorator(fn: StepHandler) -> StepHandler:
        STEP_HANDLERS[(workflow_type, step_name)] = fn
        return fn

    return decorator


def _new_run_id() -> str:
    return f'WF-{uuid.uuid4().hex[:16].upper()}'


class WorkflowEngine:
    """Durable runner. One instance per process is enough."""

    def __init__(self, db: Session, services: dict[str, Any] | None = None):
        self.db = db
        self.services = services or {}

    # -- Temporal-shaped API ------------------------------------------------ #
    def start(
        self,
        workflow_type: str,
        *,
        tenant_id: str,
        store_id: str = '',
        conversation_id: str = '',
        workflow_input: dict | None = None,
        idempotency_key: str = '',
        trace_id: str = '',
    ) -> WorkflowRun:
        if workflow_type not in WORKFLOWS:
            raise WorkflowError(f'unknown workflow type {workflow_type!r}')

        if idempotency_key:
            existing = (
                self.db.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.tenant_id == tenant_id,
                        WorkflowRun.idempotency_key == idempotency_key,
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                log_event(logger, 'workflow_start_replayed', run_id=existing.id,
                          workflow_type=workflow_type)
                return existing

        run = WorkflowRun(
            id=_new_run_id(),
            tenant_id=tenant_id,
            store_id=store_id,
            conversation_id=conversation_id,
            workflow_type=workflow_type,
            status='running',
            current_step=WORKFLOWS[workflow_type][0] if WORKFLOWS[workflow_type] else '',
            input=workflow_input or {},
            state={},
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        log_event(logger, 'workflow_started', run_id=run.id, workflow_type=workflow_type)
        return run

    async def run(self, run: WorkflowRun) -> WorkflowRun:
        """Advance the run until it completes, fails or parks in ``waiting``."""
        workflow_type = run.workflow_type
        steps = WORKFLOWS.get(workflow_type, [])
        if run.status not in ('running', 'waiting'):
            return run

        if run.status == 'waiting':
            # A parked run only advances when something external un-parked it.
            # Without a signal, re-entering run() must be a no-op — otherwise a
            # sweeper retry would silently skip the human decision.
            if not (run.state or {}).get('_signals'):
                return run
            self._assert_transition(run, 'running')

        ctx = StepContext(run=run, db=self.db, services=self.services,
                          state=dict(run.state or {}))
        start_index = self._next_step_index(run, steps)

        for index in range(start_index, len(steps)):
            name = steps[index]
            handler = STEP_HANDLERS.get((workflow_type, name))
            if handler is None:
                raise WorkflowError(f'no handler for {workflow_type}.{name}')

            run.current_step = name
            self.db.commit()

            outcome = await handler(ctx)
            ctx.state.update(outcome.patch)
            run.state = dict(ctx.state)
            self._checkpoint(run, index, name)

            if outcome.kind == 'wait':
                self._assert_transition(run, 'waiting')
                self.db.commit()
                log_event(logger, 'workflow_waiting', run_id=run.id, step=name,
                          reason=outcome.detail)
                return run
            if outcome.kind == 'fail':
                self._assert_transition(run, 'failed')
                run.error = outcome.detail
                self.db.commit()
                log_event(logger, 'workflow_failed', level=logging.ERROR, run_id=run.id,
                          step=name, reason=outcome.detail)
                return run

        self._assert_transition(run, 'completed')
        run.result = dict(ctx.state)
        self.db.commit()
        self.db.refresh(run)
        log_event(logger, 'workflow_completed', run_id=run.id, workflow_type=workflow_type)
        return run

    def signal(self, run_id: str, name: str, payload: dict | None = None) -> WorkflowRun:
        """Deliver an external event to a parked run and un-park it."""
        run = self.db.get(WorkflowRun, run_id)
        if run is None:
            raise WorkflowError(f'run {run_id!r} not found')
        if run.status in ('completed', 'failed', 'cancelled'):
            log_event(logger, 'workflow_signal_ignored', level=logging.WARNING,
                      run_id=run_id, status=run.status, signal=name)
            return run
        state = dict(run.state or {})
        signals = dict(state.get('_signals') or {})
        signals[name] = payload or {}
        state['_signals'] = signals
        run.state = state
        self.db.commit()
        self.db.refresh(run)
        log_event(logger, 'workflow_signalled', run_id=run_id, signal=name)
        return run

    def query(self, run_id: str) -> dict:
        run = self.db.get(WorkflowRun, run_id)
        if run is None:
            raise WorkflowError(f'run {run_id!r} not found')
        return {
            'run_id': run.id,
            'workflow_type': run.workflow_type,
            'status': run.status,
            'current_step': run.current_step,
            'state': run.state or {},
            'error': run.error,
            'updated_at': run.updated_at.isoformat() if run.updated_at else '',
        }

    def get_result(self, run_id: str) -> dict:
        run = self.db.get(WorkflowRun, run_id)
        if run is None:
            raise WorkflowError(f'run {run_id!r} not found')
        return {
            'run_id': run.id,
            'status': run.status,
            'result': run.result or {},
            'error': run.error,
        }

    def cancel(self, run_id: str) -> WorkflowRun:
        run = self.db.get(WorkflowRun, run_id)
        if run is None:
            raise WorkflowError(f'run {run_id!r} not found')
        if run.status in ('completed', 'failed', 'cancelled'):
            return run
        self._assert_transition(run, 'cancelled')
        self.db.commit()
        self.db.refresh(run)
        return run

    # -- internals ---------------------------------------------------------- #
    def _assert_transition(self, run: WorkflowRun, target: str) -> None:
        from .. import metrics

        if target not in RUN_TRANSITIONS.get(run.status, set()):
            metrics.STATE_MACHINE_VIOLATIONS.labels('workflow_run', run.status, target).inc()
            raise WorkflowError(f'workflow_run: {run.status} -> {target} is not allowed')
        run.status = target

    def _checkpoint(self, run: WorkflowRun, index: int, name: str) -> None:
        existing = (
            self.db.execute(
                select(WorkflowCheckpoint).where(
                    WorkflowCheckpoint.run_id == run.id, WorkflowCheckpoint.step_index == index
                )
            )
            .scalars()
            .first()
        )
        state = dict(run.state or {})
        if existing is None:
            self.db.add(WorkflowCheckpoint(
                run_id=run.id, step_index=index, step_name=name, state=state
            ))
        else:
            existing.state = state
            existing.step_name = name
        self.db.commit()

    def _next_step_index(self, run: WorkflowRun, steps: list[str]) -> int:
        if run.status == 'running' and not run.current_step:
            return 0
        last = (
            self.db.execute(
                select(WorkflowCheckpoint)
                .where(WorkflowCheckpoint.run_id == run.id)
                .order_by(WorkflowCheckpoint.step_index.desc())
            )
            .scalars()
            .first()
        )
        if last is None:
            return 0
        # Resume *after* the last checkpointed step.
        return last.step_index + 1


async def resume_parked_runs(
    db: Session,
    services: dict[str, Any] | None = None,
    *,
    limit: int = 20,
    tenant_id: str = '',
) -> list[str]:
    """Sweeper entry point: advance runs that were signalled while parked.

    Called by ``scripts/workflow_sweeper.py`` (and by the approval endpoint
    right after a decision), so a turn parked on human approval continues
    without a second user message.
    """
    engine = WorkflowEngine(db, services)
    stmt = select(WorkflowRun).where(WorkflowRun.status == 'waiting').limit(limit)
    if tenant_id:
        stmt = stmt.where(WorkflowRun.tenant_id == tenant_id)

    resumed: list[str] = []
    for run in list(db.execute(stmt).scalars()):
        if not (run.state or {}).get('_signals'):
            continue
        try:
            await engine.run(run)
            resumed.append(run.id)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            log_event(logger, 'workflow_resume_failed', level=logging.ERROR,
                      run_id=run.id, error=type(exc).__name__, message=str(exc)[:300])
    return resumed
