# -*- coding: utf-8 -*-
"""LLM adapter behaviour — the parts that must never take the trunk down.

Three properties are load-bearing and therefore tested directly:

1. **It degrades, it never raises.** A dead provider must produce a failed
   result object, not a 500. Every failure mode below is asserted to return
   rather than propagate.
2. **Thinking mode cannot silently eat the answer.** DeepSeek V4 defaults to
   thinking, which spends the completion budget on ``reasoning_content``. At the
   intent classifier's ``max_tokens=80`` that can consume the entire budget and
   return an empty ``content`` — which the caller would try to JSON-parse and
   give up on, silently disabling the fallback. It has its own error code.
3. **Cost is metered honestly.** Cache hits are ~50x cheaper than misses on
   input; pricing every prompt token at the miss rate overstates cost badly once
   the system prompt is warm.
"""
from __future__ import annotations

import json

import pytest

from gateway.app.config import Settings
from gateway.app.core.llm import (
    BREAKER_THRESHOLD,
    LLMClient,
    parse_reasoning_tokens,
    parse_usage,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class _Stream:
    """Minimal async context manager matching ``client.stream(...)``."""

    def __init__(self, status=200, lines=(), text=''):
        self.status_code = status
        self._lines = list(lines)
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aread(self):
        return self.text.encode()

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _install(monkeypatch, responses):
    """Script ``httpx.AsyncClient``; return the log of request bodies."""
    from gateway.app.core import llm as llm_mod

    log: list[dict] = []

    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, headers=None, json=None):  # noqa: A002
            log.append(json)
            if not responses:
                raise AssertionError('unexpected extra HTTP call')
            return responses.pop(0)

        def stream(self, _method, _url, headers=None, json=None):  # noqa: A002
            log.append(json)
            if not responses:
                raise AssertionError('unexpected extra stream call')
            return responses.pop(0)

    monkeypatch.setattr(llm_mod.httpx, 'AsyncClient', _Client)
    return log


def _ok(text='ok', usage=None):
    return _Resp(200, {
        'model': 'deepseek-flash',
        'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
        'usage': usage or {'prompt_tokens': 10, 'completion_tokens': 2},
    })


@pytest.fixture()
def llm() -> LLMClient:
    return LLMClient(Settings(
        llm_enabled=True,
        llm_api_key='test-key',
        llm_base_url='https://api.deepseek.com',
        llm_model='deepseek-flash',
        llm_thinking='disabled',
        llm_price_in_per_1k=0.0003,
        llm_price_in_cached_per_1k=0.000006,
        llm_price_out_per_1k=0.0012,
    ))


# --------------------------------------------------------------------------- #
# usage parsing
# --------------------------------------------------------------------------- #
def test_parse_usage_handles_deepseek_shape():
    assert parse_usage({
        'prompt_tokens': 100, 'completion_tokens': 20,
        'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 20,
    }) == (100, 20, 80, 20)


def test_parse_usage_handles_openai_shape():
    assert parse_usage({
        'prompt_tokens': 100, 'completion_tokens': 20,
        'prompt_tokens_details': {'cached_tokens': 64},
    }) == (100, 20, 64, 36)


def test_parse_usage_without_cache_info_treats_all_as_miss():
    """The conservative default: a miss is the more expensive of the two."""
    assert parse_usage({'prompt_tokens': 100, 'completion_tokens': 20}) == (100, 20, 0, 100)


def test_parse_reasoning_tokens():
    assert parse_reasoning_tokens(
        {'completion_tokens_details': {'reasoning_tokens': 17}}) == 17
    assert parse_reasoning_tokens({}) == 0


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def test_cache_hits_are_priced_far_below_misses(llm):
    all_hit = llm._price(1_000_000, 0, cache_hit=1_000_000, cache_miss=0)
    all_miss = llm._price(1_000_000, 0, cache_hit=0, cache_miss=1_000_000)
    assert all_miss == pytest.approx(0.3)
    assert all_hit == pytest.approx(0.006)
    assert all_miss / all_hit == pytest.approx(50)


def test_output_tokens_are_priced_at_the_output_rate(llm):
    assert llm._price(0, 1_000_000) == pytest.approx(1.2)


def test_mixed_prompt_sums_both_input_rates(llm):
    cost = llm._price(1000, 0, cache_hit=400, cache_miss=600)
    assert cost == pytest.approx(400 / 1000 * 0.000006 + 600 / 1000 * 0.0003)


# --------------------------------------------------------------------------- #
# disabled / degrade paths
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_disabled_client_makes_no_http_call(monkeypatch):
    client = LLMClient(Settings(llm_enabled=False, llm_api_key=''))
    assert client.available is False
    log = _install(monkeypatch, [])
    result = await client.chat([{'role': 'user', 'content': 'hi'}])
    assert result.error == 'llm_disabled'
    assert log == [], 'a disabled client must not touch the network'


@pytest.mark.asyncio
async def test_http_error_returns_a_result_and_does_not_raise(llm, monkeypatch):
    _install(monkeypatch, [_Resp(500, text='upstream boom')])
    result = await llm.chat([{'role': 'user', 'content': 'hi'}])
    assert result.ok is False
    assert result.error == 'http_500'


@pytest.mark.asyncio
async def test_transport_exception_is_swallowed(llm, monkeypatch):
    from gateway.app.core import llm as llm_mod

    class _Boom:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_a, **_k):
            raise TimeoutError('read timeout')

    monkeypatch.setattr(llm_mod.httpx, 'AsyncClient', _Boom)
    result = await llm.chat([{'role': 'user', 'content': 'hi'}])
    assert result.ok is False
    assert result.error == 'TimeoutError'


@pytest.mark.asyncio
async def test_circuit_breaker_opens_then_stops_calling(llm, monkeypatch):
    _install(monkeypatch, [_Resp(500, text='boom') for _ in range(BREAKER_THRESHOLD)])
    for _ in range(BREAKER_THRESHOLD):
        await llm.chat([{'role': 'user', 'content': 'hi'}])

    assert llm.status()['circuit_open'] is True

    # With the breaker open the call short-circuits: no HTTP request is made,
    # which is the whole point of having one.
    log = _install(monkeypatch, [])
    result = await llm.chat([{'role': 'user', 'content': 'hi'}])
    assert result.error == 'circuit_open'
    assert log == []


@pytest.mark.asyncio
async def test_success_resets_the_failure_counter(llm, monkeypatch):
    _install(monkeypatch, [_Resp(500, text='x'), _ok('fine')])
    await llm.chat([{'role': 'user', 'content': 'a'}])
    assert llm.status()['consecutive_failures'] == 1
    await llm.chat([{'role': 'user', 'content': 'b'}])
    assert llm.status()['consecutive_failures'] == 0


# --------------------------------------------------------------------------- #
# thinking mode
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_thinking_consuming_the_budget_is_its_own_error(llm, monkeypatch):
    """Empty ``content`` plus non-empty ``reasoning_content`` is not an answer."""
    _install(monkeypatch, [_Resp(200, {
        'model': 'deepseek-flash',
        'choices': [{
            'message': {'content': '', 'reasoning_content': '推理过程' * 50},
            'finish_reason': 'length',
        }],
        'usage': {'prompt_tokens': 34, 'completion_tokens': 80},
    })])

    result = await llm.chat([{'role': 'user', 'content': 'hi'}], max_tokens=80)
    assert result.ok is False
    assert result.error == 'thinking_consumed_budget'
    # Tokens were still spent, so the counters must not pretend otherwise.
    assert result.tokens_out == 80


@pytest.mark.asyncio
async def test_thinking_is_sent_when_configured(llm, monkeypatch):
    log = _install(monkeypatch, [_ok()])
    await llm.chat([{'role': 'user', 'content': 'hi'}])
    assert log[0]['thinking'] == {'type': 'disabled'}


@pytest.mark.asyncio
async def test_thinking_is_omitted_when_auto(monkeypatch):
    client = LLMClient(Settings(
        llm_enabled=True, llm_api_key='k', llm_base_url='https://example.test',
        llm_thinking='auto',
    ))
    log = _install(monkeypatch, [_ok()])
    await client.chat([{'role': 'user', 'content': 'hi'}])
    assert 'thinking' not in log[0], 'auto must send no thinking key at all'


@pytest.mark.asyncio
async def test_rejected_thinking_key_is_retried_once_without_it(llm, monkeypatch):
    """An endpoint that rejects ``thinking`` must cost one retry, not every call."""
    log = _install(monkeypatch, [
        _Resp(400, text='{"error":"unknown field thinking"}'),
        _ok('recovered'),
    ])

    result = await llm.chat([{'role': 'user', 'content': 'hi'}])
    assert result.ok is True
    assert result.text == 'recovered'
    assert llm._thinking_supported is False

    assert 'thinking' in log[0]
    assert 'thinking' not in log[1], 'the retry must drop the offending key'


@pytest.mark.asyncio
async def test_thinking_is_not_resent_after_being_rejected(llm, monkeypatch):
    """The downgrade is remembered, so later calls skip the failed round trip."""
    _install(monkeypatch, [
        _Resp(400, text='nope'),
        _ok('first'),
    ])
    await llm.chat([{'role': 'user', 'content': 'a'}])

    log = _install(monkeypatch, [_ok('second')])
    await llm.chat([{'role': 'user', 'content': 'b'}])
    assert 'thinking' not in log[0]


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stream_yields_deltas_then_done(llm, monkeypatch):
    _install(monkeypatch, [_Stream(200, lines=[
        'data: ' + json.dumps({'choices': [{'delta': {'content': '你'}}]}),
        'data: ' + json.dumps({'choices': [{'delta': {'content': '好'}}]}),
        'data: ' + json.dumps({'usage': {'prompt_tokens': 7, 'completion_tokens': 2}}),
        'data: [DONE]',
    ])])

    events = [e async for e in llm.chat_stream([{'role': 'user', 'content': 'hi'}])]
    kinds = [k for k, _ in events]
    assert kinds == ['delta', 'delta', 'done']
    assert ''.join(p for k, p in events if k == 'delta') == '你好'


@pytest.mark.asyncio
async def test_stream_does_not_leak_reasoning_to_the_user(llm, monkeypatch):
    """Chain-of-thought must never be forwarded as if it were the answer."""
    _install(monkeypatch, [_Stream(200, lines=[
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': '内部推理'}}]}),
        'data: ' + json.dumps({'choices': [{'delta': {'content': '正式回答'}}]}),
        'data: [DONE]',
    ])])

    events = [e async for e in llm.chat_stream([{'role': 'user', 'content': 'hi'}])]
    streamed = ''.join(p for k, p in events if k == 'delta')
    assert streamed == '正式回答'
    assert '内部推理' not in streamed


@pytest.mark.asyncio
async def test_stream_reports_thinking_budget_exhaustion(llm, monkeypatch):
    _install(monkeypatch, [_Stream(200, lines=[
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': '想' * 100}}]}),
        'data: [DONE]',
    ])])

    events = [e async for e in llm.chat_stream([{'role': 'user', 'content': 'hi'}])]
    assert events[-1] == ('error', 'thinking_consumed_budget')


@pytest.mark.asyncio
async def test_stream_error_is_reported_not_raised(llm, monkeypatch):
    _install(monkeypatch, [_Stream(503, text='unavailable')])
    events = [e async for e in llm.chat_stream([{'role': 'user', 'content': 'hi'}])]
    assert events == [('error', 'http_503')]


# --------------------------------------------------------------------------- #
# intent node
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_classify_intent_parses_a_valid_reply(llm, monkeypatch):
    _install(monkeypatch, [_ok(json.dumps({'intent': 'refund', 'confidence': 0.91}))])
    intent, confidence, result = await llm.classify_intent('我要退款', ['refund', 'logistics'])
    assert (intent, confidence) == ('refund', 0.91)
    assert result.ok


@pytest.mark.asyncio
async def test_classify_intent_refuses_a_non_candidate(llm, monkeypatch):
    """A model answer outside the candidate set is "still unsure", not a guess."""
    _install(monkeypatch, [_ok(json.dumps({'intent': 'invented', 'confidence': 0.99}))])
    intent, confidence, _ = await llm.classify_intent('嗯', ['refund', 'logistics'])
    assert (intent, confidence) == ('', 0.0)


@pytest.mark.asyncio
async def test_classify_intent_survives_unparseable_json(llm, monkeypatch):
    _install(monkeypatch, [_ok('抱歉，我无法判断')])
    intent, confidence, _ = await llm.classify_intent('嗯', ['refund'])
    assert (intent, confidence) == ('', 0.0)


@pytest.mark.asyncio
async def test_classify_intent_clamps_out_of_range_confidence(llm, monkeypatch):
    _install(monkeypatch, [_ok(json.dumps({'intent': 'refund', 'confidence': 7}))])
    _, confidence, _ = await llm.classify_intent('退款', ['refund'])
    assert confidence == 1.0


# --------------------------------------------------------------------------- #
# status surface
# --------------------------------------------------------------------------- #
def test_status_is_informative_and_side_effect_free(llm):
    status = llm.status()
    assert status['available'] is True
    assert status['model'] == 'deepseek-flash'
    assert status['thinking'] == 'disabled'
    assert status['circuit_open'] is False
    # Calling status() must not half-open a tripped breaker as a side effect.
    assert llm.status()['consecutive_failures'] == 0


def test_status_of_a_disabled_client_hides_configuration():
    client = LLMClient(Settings(llm_enabled=False, llm_api_key='', llm_base_url=''))
    status = client.status()
    assert status['available'] is False
    assert status['model'] == ''
    assert status['base_url'] == ''
