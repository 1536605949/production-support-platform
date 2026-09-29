# -*- coding: utf-8 -*-
"""Contract tests for the tenant operations console (``/api/admin/overview``).

The console is the *tenant-scoped* view: it answers "how is this merchant
doing" from the tables the trunk wrote. Prometheus deliberately does not serve
these numbers, because a per-tenant label is unbounded cardinality.

These tests pin the fields the frontend depends on, and — more importantly —
the two derivations that are easy to get subtly wrong: the cache-hit rate (a
*subset* of input tokens, not a third bucket) and the daily series (which must
be gap-free, or a quiet day silently disappears from the chart).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from gateway.app.db import SessionLocal, engine
from gateway.app.models import Conversation, Message

# The fields the metrics page renders. A rename here breaks the UI silently —
# ``undefined`` renders as an empty card rather than an error — so they are
# asserted by name.
EXPECTED_FIELDS = {
    'tenant_id', 'window_days', 'conversations', 'messages', 'answers',
    'completion_rate', 'degraded_answers', 'degraded_rate',
    'handoff_conversations', 'handoff_rate',
    'ttft_ms_p50', 'ttft_ms_p95', 'latency_ms_p50', 'latency_ms_p95',
    'tokens_in', 'tokens_out', 'cache_hit_tokens', 'cache_hit_rate', 'cost_usd',
    'intent_distribution', 'intent_source', 'compose_source',
    'tool_status', 'approval_status', 'daily', 'quota',
}


def _overview(client, headers, days=7):
    resp = client.get(f'/api/admin/overview?days={days}', headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_overview_exposes_every_field_the_console_renders(client, dev_headers):
    body = _overview(client, dev_headers)
    missing = EXPECTED_FIELDS - set(body)
    assert not missing, f'前端依赖但这些字段缺失: {sorted(missing)}'


def test_daily_series_is_gap_free_and_covers_the_window(client, dev_headers):
    """Empty days must be present as zeros.

    A sparse series compresses the x-axis, so a day with no traffic vanishes and
    the chart reads as "steady" when the truth is "nothing happened".
    """
    body = _overview(client, dev_headers, days=7)
    daily = body['daily']
    assert len(daily) == 8, f'7 天窗口应含 8 个日期点（含今天），实际 {len(daily)}'
    days = [d['day'] for d in daily]
    assert days == sorted(days), 'daily 必须按日期升序'
    for entry in daily:
        assert set(entry) == {'day', 'messages', 'answers', 'degraded', 'cost_usd'}
        assert entry['degraded'] <= entry['answers'] <= entry['messages']


def test_window_days_is_echoed_and_bounded(client, dev_headers):
    assert _overview(client, dev_headers, days=1)['window_days'] == 1
    assert _overview(client, dev_headers, days=30)['window_days'] == 30
    # Out of range must be rejected, not silently clamped: a UI that asks for
    # 365 days and gets 90 without being told would mislabel the axis.
    assert client.get('/api/admin/overview?days=0', headers=dev_headers).status_code == 422
    assert client.get('/api/admin/overview?days=400', headers=dev_headers).status_code == 422


def test_cache_hit_tokens_is_a_subset_of_input_tokens(client, dev_headers):
    """The cache-hit count must never exceed the prompt tokens.

    It is recorded per message in ``meta.cache_hit_tokens`` and is a *subset* of
    ``tokens_in`` (cache hits are prompt tokens, billed ~50x cheaper). Summing it
    into a separate bucket, or double counting it, would make the rate exceed 100%
    — which is the shape of the bug this assertion is here to catch.
    """
    body = _overview(client, dev_headers)
    assert 0 <= body['cache_hit_tokens'] <= body['tokens_in']
    assert 0.0 <= body['cache_hit_rate'] <= 1.0


def test_cache_hit_rate_is_derived_from_the_counters(client, dev_headers):
    body = _overview(client, dev_headers)
    if body['tokens_in']:
        expected = round(body['cache_hit_tokens'] / body['tokens_in'], 4)
        assert body['cache_hit_rate'] == expected
    else:
        assert body['cache_hit_rate'] == 0.0


def test_degraded_rate_matches_its_counterpart(client, dev_headers):
    body = _overview(client, dev_headers)
    if body['answers']:
        assert body['degraded_rate'] == round(body['degraded_answers'] / body['answers'], 4)
        assert round(body['completion_rate'] + body['degraded_rate'], 4) == 1.0
    else:
        assert body['degraded_rate'] == 0.0


def test_source_breakdowns_cover_every_answer(client, dev_headers):
    """Every stored answer must land in exactly one bucket of each breakdown.

    ``unknown`` exists so a message written before the field was persisted still
    gets counted. If the buckets summed to less than ``answers``, the console
    would quietly under-report how often the model was used.
    """
    body = _overview(client, dev_headers)
    assert sum(body['intent_source'].values()) == body['answers']
    assert sum(body['compose_source'].values()) == body['answers']


def test_overview_is_scoped_to_the_calling_tenant(client, dev_headers, other_tenant_headers):
    """A tenant must not see another tenant's numbers."""
    mine = _overview(client, dev_headers)
    theirs = _overview(client, other_tenant_headers)
    assert mine['tenant_id'] == 'tenant-demo'
    assert theirs['tenant_id'] == 'tenant-other'
    # The other tenant has no seeded traffic; its window must be empty rather
    # than a copy of the demo tenant's.
    assert theirs['answers'] == 0
    assert theirs['messages'] == 0


def test_daily_buckets_are_filled_from_persisted_messages(client, dev_headers):
    """A message written today must appear in today's bucket.

    This is the join between persistence and the console: if the bucket key or
    the timezone handling drifted, the trend would stay flat at zero forever
    while every other number looked correct.
    """
    session = SessionLocal()
    try:
        conversation = Conversation(
            id='CONV-METRICS-1', tenant_id='tenant-demo', store_id='store-demo',
            user_id='user001', title='metrics probe', status='active',
            last_message_at=datetime.now(timezone.utc),
        )
        session.merge(conversation)
        session.add(Message(
            id='MSG-METRICS-1', conversation_id='CONV-METRICS-1',
            tenant_id='tenant-demo', seq=1, role='assistant', content='探针',
            intent='order_query', confidence=0.9, trace_id='trace-metrics',
            tokens_in=100, tokens_out=10, cost_usd=0.0001,
            meta={'intent_source': 'deterministic', 'compose_source': 'deterministic',
                  'degraded': False, 'cache_hit_tokens': 40},
        ))
        session.commit()
    finally:
        session.close()

    try:
        body = _overview(client, dev_headers, days=1)
        today = datetime.now(timezone.utc).date().isoformat()
        bucket = [d for d in body['daily'] if d['day'] == today]
        assert bucket, f'daily 缺少今天的分桶: {[d["day"] for d in body["daily"]]}'
        assert bucket[0]['answers'] >= 1
        # The 40 cached tokens on this message must show up in the aggregate.
        assert body['cache_hit_tokens'] >= 40
        assert body['compose_source'].get('deterministic', 0) >= 1
    finally:
        session = SessionLocal()
        try:
            message = session.get(Message, 'MSG-METRICS-1')
            if message is not None:
                session.delete(message)
            conversation = session.get(Conversation, 'CONV-METRICS-1')
            if conversation is not None:
                session.delete(conversation)
            session.commit()
        finally:
            session.close()


def test_daily_window_start_is_inclusive(client, dev_headers):
    """The oldest bucket must be exactly ``window_days`` ago, not one more."""
    body = _overview(client, dev_headers, days=3)
    expected_first = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    assert body['daily'][0]['day'] == expected_first


# --------------------------------------------------------------------------- #
# Query shape
#
# The console reads the table that grows fastest, over a user-chosen window.
# It used to do that by selecting whole ORM entities, which drags in two wide
# columns that no number on the page uses:
#
#   content  -- the full answer text
#   sources  -- every retrieved passage, with scores
#
# Neither appears in the response. They were read only because ``select(Message)``
# reads everything. On a 4 GB host that is the difference between an endpoint
# that survives a busy month and one that gets OOM-killed, and the failure would
# show up as "the console is slow" rather than as anything pointing here.
#
# Asserting on the emitted SQL is deliberate: the cheap way to reintroduce the
# problem is a one-line edit back to ``select(Message)``, and no behavioural
# test would notice, because the response is identical.
# --------------------------------------------------------------------------- #
_WIDE_COLUMNS = ('messages.content', 'messages.sources')


def _capture_statements(fn):
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', _record)
    try:
        fn()
    finally:
        event.remove(engine, 'before_cursor_execute', _record)
    return statements


def test_overview_never_reads_the_wide_message_columns(client, dev_headers):
    statements = _capture_statements(lambda: _overview(client, dev_headers))
    touching_messages = [s for s in statements if 'messages' in s.lower()]
    assert touching_messages, '预期至少有一条查询会读 messages'
    for sql in touching_messages:
        lowered = sql.lower()
        for column in _WIDE_COLUMNS:
            assert column not in lowered, (
                f'/api/admin/overview 读了不被使用的宽列 {column}——'
                f'这会随流量线性放大内存占用：\n{sql[:400]}'
            )


def test_overview_counts_conversations_in_sql_not_in_python(client, dev_headers):
    """Conversation totals must be aggregated by the database.

    ``conversations`` and ``handoff_conversations`` are counts. Selecting the
    rows to count them in Python is O(rows) memory for an O(1) answer.
    """
    statements = _capture_statements(lambda: _overview(client, dev_headers))
    conversation_sql = [s for s in statements if 'conversations' in s.lower()]
    assert conversation_sql, '预期有一条查询会读 conversations'
    assert any(
        'count(' in s.lower() for s in conversation_sql
    ), f'会话计数没有下推到 SQL：{[s[:200] for s in conversation_sql]}'
