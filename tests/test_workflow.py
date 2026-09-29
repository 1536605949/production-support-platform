# -*- coding: utf-8 -*-
"""Persistent workflow shell: state machine, checkpoints, signals, resume."""
import pytest

from gateway.app.core.workflow import (
    RUN_TRANSITIONS,
    StepOutcome,
    WorkflowEngine,
    WorkflowError,
    define_workflow,
    step,
)

TEST_WF = 'test_pipeline'
define_workflow(TEST_WF, ['one', 'two', 'three'])

ORDER: list[str] = []


@step(TEST_WF, 'one')
async def _one(ctx):
    ORDER.append('one')
    return StepOutcome.proceed(value=1)


@step(TEST_WF, 'two')
async def _two(ctx):
    ORDER.append('two')
    if ctx.run.input.get('park'):
        # Park until an external signal arrives (this is the approval shape).
        return StepOutcome.wait('needs_signal', parked=True)
    return StepOutcome.proceed(value=ctx.state.get('value', 0) + 1)


@step(TEST_WF, 'three')
async def _three(ctx):
    ORDER.append('three')
    return StepOutcome.done(total=ctx.state.get('value', 0) + 10)


def test_state_machine_table_is_explicit():
    assert RUN_TRANSITIONS['completed'] == set()
    assert 'cancelled' in RUN_TRANSITIONS['running']


def test_run_completes_and_checkpoints_every_step(db_session):
    ORDER.clear()
    engine = WorkflowEngine(db_session)
    run = engine.start(TEST_WF, tenant_id='tenant-demo', workflow_input={})
    run = _run(engine, run)

    assert run.status == 'completed'
    assert run.result["total"] == 12
    assert ORDER == ['one', 'two', 'three']

    from gateway.app.models import WorkflowCheckpoint
    from sqlalchemy import select
    steps = list(db_session.execute(
        select(WorkflowCheckpoint).where(WorkflowCheckpoint.run_id == run.id)
        .order_by(WorkflowCheckpoint.step_index)
    ).scalars())
    assert [s.step_name for s in steps] == ['one', 'two', 'three']


def test_parked_run_waits_then_resumes_after_a_signal(db_session):
    ORDER.clear()
    engine = WorkflowEngine(db_session)
    run = engine.start(TEST_WF, tenant_id='tenant-demo', workflow_input={'park': True})
    run = _run(engine, run)

    assert run.status == 'waiting'
    assert run.current_step == 'two'
    assert ORDER == ['one', 'two']

    # A second attempt without a signal must not advance.
    run = _run(engine, run)
    assert run.status == 'waiting'
    assert ORDER == ['one', 'two']

    engine.signal(run.id, 'approved', {'ok': True})
    run = _run(engine, run)
    assert run.status == 'completed'
    assert ORDER == ['one', 'two', 'three']


def test_start_is_idempotent_on_the_same_key(db_session):
    engine = WorkflowEngine(db_session)
    first = engine.start(TEST_WF, tenant_id='tenant-demo', idempotency_key='k-1')
    second = engine.start(TEST_WF, tenant_id='tenant-demo', idempotency_key='k-1')
    assert first.id == second.id


def test_query_and_get_result(db_session):
    engine = WorkflowEngine(db_session)
    run = engine.start(TEST_WF, tenant_id='tenant-demo', workflow_input={})
    run = _run(engine, run)

    snapshot = engine.query(run.id)
    assert snapshot['status'] == 'completed'
    assert snapshot['workflow_type'] == TEST_WF

    result = engine.get_result(run.id)
    assert result["result"]["total"] == 12


def test_cancelling_a_finished_run_is_a_noop(db_session):
    engine = WorkflowEngine(db_session)
    run = engine.start(TEST_WF, tenant_id='tenant-demo', workflow_input={})
    run = _run(engine, run)
    assert engine.cancel(run.id).status == 'completed'


def test_unknown_workflow_type_is_rejected(db_session):
    engine = WorkflowEngine(db_session)
    with pytest.raises(WorkflowError):
        engine.start('no_such_workflow', tenant_id='tenant-demo')


def test_unknown_run_id_is_rejected(db_session):
    engine = WorkflowEngine(db_session)
    with pytest.raises(WorkflowError):
        engine.query('WF-DOES-NOT-EXIST')


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run(engine: WorkflowEngine, run):
    """Drive one workflow to its next stopping point."""
    import asyncio

    return asyncio.run(engine.run(run))
