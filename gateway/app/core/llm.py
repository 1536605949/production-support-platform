# -*- coding: utf-8 -*-
"""LLM adapter — OpenAI-compatible, optional, and never on the critical path.

The architecture puts the model at *specific nodes* only. This client is the
single door to it, and it is built to fail quietly:

* disabled (no key) → :meth:`LLMClient.available` is ``False`` and callers take
  the deterministic branch;
* upstream error or timeout → returns ``None`` instead of raising, so a
  conversation degrades rather than 500s;
* repeated failures → a small circuit breaker stops hammering a dead provider.

Every call is metered (tokens + estimated cost) because the cost metric is a
first-class requirement, not an afterthought.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from .observability import get_context, log_event

logger = logging.getLogger('gateway.llm')

BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECONDS = 60.0


@dataclass
class LLMResult:
    text: str = ''
    tokens_in: int = 0
    tokens_out: int = 0
    #: DeepSeek bills cache hits ~50x cheaper than misses on input, so the two
    #: are tracked apart — pricing every prompt token at the miss rate would
    #: overstate cost substantially once the system prompt is warm.
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    #: Tokens spent on ``reasoning_content`` (thinking mode). Not billed
    #: differently, but the count is what makes a "why was this slow" question
    #: answerable without guessing.
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    model: str = ''
    error: str = ''
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def parse_usage(usage: dict) -> tuple[int, int, int, int]:
    """Return ``(tokens_in, tokens_out, cache_hit, cache_miss)`` from a usage block.

    DeepSeek reports cache hits explicitly (``prompt_cache_hit_tokens`` /
    ``prompt_cache_miss_tokens``); OpenAI-style providers only expose
    ``prompt_tokens_details.cached_tokens``. Both shapes are handled, and when
    neither is present the whole prompt is treated as a cache miss — the
    conservative assumption, since a miss is the more expensive of the two.
    """
    tokens_in = int(usage.get('prompt_tokens') or 0)
    tokens_out = int(usage.get('completion_tokens') or 0)

    hit = int(usage.get('prompt_cache_hit_tokens') or 0)
    miss = int(usage.get('prompt_cache_miss_tokens') or 0)
    if not hit and not miss:
        hit = int((usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
        miss = max(0, tokens_in - hit)
    elif not miss:
        miss = max(0, tokens_in - hit)
    return tokens_in, tokens_out, hit, miss


def parse_reasoning_tokens(usage: dict) -> int:
    details = usage.get('completion_tokens_details') or {}
    return int(details.get('reasoning_tokens') or 0)


class LLMClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.available = bool(
            settings.llm_enabled and settings.llm_api_key and settings.llm_base_url
        )
        self._failures = 0
        self._open_until = 0.0
        #: ``None`` = untested. Flipped to ``False`` the first time the provider
        #: rejects a ``thinking`` key, so an incompatible endpoint costs exactly
        #: one failed request rather than one per call.
        self._thinking_supported: bool | None = None

    # -- internals --------------------------------------------------------- #
    def _breaker_open(self) -> bool:
        if self._failures < BREAKER_THRESHOLD:
            return False
        if time.time() >= self._open_until:
            # Half-open: let the next call through to probe the provider.
            self._failures = BREAKER_THRESHOLD - 1
            return False
        return True

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= BREAKER_THRESHOLD:
            self._open_until = time.time() + BREAKER_COOLDOWN_SECONDS
            log_event(logger, 'llm_circuit_open', level=logging.WARNING,
                      cooldown_seconds=BREAKER_COOLDOWN_SECONDS)

    def _record_success(self) -> None:
        self._failures = 0

    def _price(self, tokens_in: int, tokens_out: int,
               cache_hit: int = 0, cache_miss: int = 0) -> float:
        s = self.s
        if cache_hit or cache_miss:
            in_cost = (
                cache_hit / 1000.0 * s.llm_price_in_cached_per_1k
                + cache_miss / 1000.0 * s.llm_price_in_per_1k
            )
        else:
            in_cost = tokens_in / 1000.0 * s.llm_price_in_per_1k
        return round(in_cost + tokens_out / 1000.0 * s.llm_price_out_per_1k, 8)

    def _thinking_payload(self) -> dict:
        """Body fragment that pins the thinking mode, if we pin one at all.

        ``auto`` sends nothing, which is the safe choice for a non-DeepSeek
        OpenAI-compatible endpoint that would reject an unknown key.
        """
        mode = (self.s.llm_thinking or 'auto').strip().lower()
        if mode not in ('enabled', 'disabled'):
            return {}
        return {'thinking': {'type': mode}}

    def status(self) -> dict:
        """Cheap, side-effect-free view for readiness probes and dashboards."""
        return {
            'available': self.available,
            'model': self.s.llm_model if self.available else '',
            'base_url': self.s.llm_base_url if self.available else '',
            'thinking': self.s.llm_thinking,
            'consecutive_failures': self._failures,
            'circuit_open': self._breaker_open_state(),
            'circuit_open_for_seconds': max(0.0, round(self._open_until - time.time(), 1)),
        }

    def _breaker_open_state(self) -> bool:
        """Like :meth:`_breaker_open` but without the half-open side effect."""
        return self._failures >= BREAKER_THRESHOLD and time.time() < self._open_until

    async def _post_chat(self, payload: dict, purpose: str) -> httpx.Response:
        """POST a completion, retrying once if the ``thinking`` key is rejected.

        A 400 caused by an unrecognised body key is a *configuration* mismatch,
        not a provider outage. Without this retry, pointing the client at an
        endpoint that does not understand ``thinking`` would pin every call to
        the deterministic branch and hide a perfectly working model behind a
        permanent fallback.
        """
        url = f'{self.s.llm_base_url.rstrip("/")}/chat/completions'
        headers = {
            'Authorization': f'Bearer {self.s.llm_api_key}',
            'Content-Type': 'application/json',
        }
        async with httpx.AsyncClient(timeout=self.s.llm_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 400 and 'thinking' in payload:
                self._thinking_supported = False
                log_event(logger, 'llm_thinking_rejected', level=logging.WARNING,
                          purpose=purpose, body=resp.text[:200])
                retry = {k: v for k, v in payload.items() if k != 'thinking'}
                resp = await client.post(url, headers=headers, json=retry)
            return resp

    async def chat(
        self,
        messages: list[dict],
        *,
        purpose: str = 'chat',
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> LLMResult:
        """One completion. Returns a failed :class:`LLMResult` instead of raising."""
        from .. import metrics

        if not self.available:
            return LLMResult(error='llm_disabled')
        if self._breaker_open():
            metrics.LLM_CALLS.labels(purpose, 'circuit_open').inc()
            return LLMResult(error='circuit_open')

        payload: dict = {
            'model': self.s.llm_model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens or self.s.llm_max_tokens,
        }
        if response_format:
            payload['response_format'] = response_format
        if self._thinking_supported is not False:
            payload.update(self._thinking_payload())

        started = time.perf_counter()
        try:
            resp = await self._post_chat(payload, purpose)
            if resp.status_code >= 400:
                self._record_failure()
                metrics.LLM_CALLS.labels(purpose, str(resp.status_code)).inc()
                log_event(logger, 'llm_error', level=logging.WARNING,
                          purpose=purpose, status=resp.status_code, body=resp.text[:300])
                return LLMResult(error=f'http_{resp.status_code}')

            data = resp.json()
            usage = data.get('usage') or {}
            tokens_in, tokens_out, cache_hit, cache_miss = parse_usage(usage)
            choices = data.get('choices') or []
            message = (choices[0].get('message') or {}) if choices else {}
            text = (message.get('content') or '').strip()
            reasoning = message.get('reasoning_content') or ''

            if not text and reasoning:
                # Thinking mode spent the whole completion budget on
                # ``reasoning_content`` and never emitted an answer. Reporting
                # this as a plain empty reply would silently break the caller:
                # the intent fallback would try to JSON-parse "" and give up.
                self._record_failure()
                metrics.LLM_CALLS.labels(purpose, 'thinking_consumed_budget').inc()
                log_event(logger, 'llm_thinking_ate_budget', level=logging.WARNING,
                          purpose=purpose, max_tokens=payload['max_tokens'],
                          reasoning_chars=len(reasoning))
                return LLMResult(error='thinking_consumed_budget',
                                 model=data.get('model', ''), tokens_in=tokens_in,
                                 tokens_out=tokens_out, cache_hit_tokens=cache_hit,
                                 cache_miss_tokens=cache_miss)

            result = LLMResult(
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                reasoning_tokens=parse_reasoning_tokens(usage),
                cost_usd=self._price(tokens_in, tokens_out, cache_hit, cache_miss),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model=data.get('model', self.s.llm_model),
                raw=data,
            )
            self._record_success()
            metrics.LLM_CALLS.labels(purpose, 'ok').inc()
            metrics.LLM_LATENCY.labels(purpose).observe(result.latency_ms / 1000.0)
            metrics.LLM_TOKENS.labels('in').inc(tokens_in)
            metrics.LLM_TOKENS.labels('out').inc(tokens_out)
            metrics.LLM_TOKENS.labels('in_cached').inc(cache_hit)
            metrics.LLM_COST.inc(result.cost_usd)
            log_event(logger, 'llm_call', purpose=purpose, model=result.model,
                      tokens_in=tokens_in, tokens_out=tokens_out,
                      cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss,
                      reasoning_tokens=result.reasoning_tokens,
                      cost_usd=result.cost_usd, latency_ms=result.latency_ms,
                      trace_id=get_context().trace_id)
            return result

        except Exception as exc:  # noqa: BLE001 - degrade, never propagate
            self._record_failure()
            metrics.LLM_CALLS.labels(purpose, 'error').inc()
            log_event(logger, 'llm_exception', level=logging.WARNING,
                      purpose=purpose, error=type(exc).__name__, message=str(exc)[:300])
            return LLMResult(error=type(exc).__name__)

    # -- node-specific helpers --------------------------------------------- #
    async def classify_intent(self, text: str, candidates: list[str]) -> tuple[str, float, LLMResult]:
        """Low-confidence fallback for the routing node.

        Asks the model to pick one of ``candidates`` and to report its own
        confidence. Anything unparseable is treated as "still unsure" rather
        than guessed at.
        """
        system = (
            '你是一个客服意图分类器。只输出 JSON，不要解释。'
            f'候选意图：{", ".join(candidates)}。'
            '输出格式：{"intent": "<候选之一>", "confidence": <0 到 1 的小数>}'
        )
        result = await self.chat(
            [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
            purpose='intent',
            temperature=0.0,
            max_tokens=80,
            response_format={'type': 'json_object'},
        )
        if not result.ok:
            return '', 0.0, result
        try:
            payload = json.loads(result.text)
            intent = str(payload.get('intent', '')).strip()
            confidence = float(payload.get('confidence', 0.0))
            if intent not in candidates:
                return '', 0.0, result
            return intent, max(0.0, min(1.0, confidence)), result
        except Exception:  # noqa: BLE001
            log_event(logger, 'llm_intent_unparseable', level=logging.WARNING,
                      raw=result.text[:200])
            return '', 0.0, result

    def build_answer_messages(
        self,
        question: str,
        context_blocks: list[str],
        *,
        history: list[dict] | None = None,
        persona: str = '你是一名专业、简洁的电商客服助手。',
        extra_instruction: str = '',
    ) -> list[dict]:
        """Grounded-answer prompt: the model may only use the supplied context."""
        context = '\n\n'.join(f'[{i + 1}] {b}' for i, b in enumerate(context_blocks))
        system = (
            f'{persona}\n'
            '只依据下面的「知识片段」和「系统事实」回答；如果不足以回答，就直接说没有找到依据，'
            '不要编造订单号、金额或政策。回答控制在 200 字以内。\n'
            f'{extra_instruction}\n\n'
            f'知识片段：\n{context or "（无）"}'
        )
        messages = [{'role': 'system', 'content': system}]
        messages.extend(history or [])
        messages.append({'role': 'user', 'content': question})
        return messages

    async def answer_with_context(
        self,
        question: str,
        context_blocks: list[str],
        *,
        history: list[dict] | None = None,
        persona: str = '你是一名专业、简洁的电商客服助手。',
        extra_instruction: str = '',
    ) -> LLMResult:
        """Grounded answer node (non-streaming)."""
        messages = self.build_answer_messages(
            question, context_blocks, history=history, persona=persona,
            extra_instruction=extra_instruction,
        )
        return await self.chat(messages, purpose='answer', temperature=0.3)

    async def chat_stream(self, messages: list[dict], *, purpose: str = 'answer',
                          temperature: float = 0.3):
        """Stream a completion, yielding ``('delta', text)`` then ``('done', result)``.

        Real provider streaming is what makes the TTFT metric meaningful: the
        first delta arrives long before the full answer does. On any failure the
        generator simply stops after yielding an ``error`` event, and the caller
        falls back to its deterministic template.

        ``delta.reasoning_content`` (thinking mode) is deliberately **not**
        forwarded. It is the model's scratchpad, not an answer — streaming it
        into a customer-facing bubble would leak chain-of-thought to the user.
        """
        from .. import metrics

        if not self.available or self._breaker_open():
            yield 'error', 'llm_unavailable'
            return

        payload: dict = {
            'model': self.s.llm_model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': self.s.llm_max_tokens,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        if self._thinking_supported is not False:
            payload.update(self._thinking_payload())

        url = f'{self.s.llm_base_url.rstrip("/")}/chat/completions'
        headers = {
            'Authorization': f'Bearer {self.s.llm_api_key}',
            'Content-Type': 'application/json',
        }
        started = time.perf_counter()
        collected: list[str] = []
        reasoning_chars = 0
        tokens_in = tokens_out = cache_hit = cache_miss = reasoning_tokens = 0

        try:
            async with httpx.AsyncClient(timeout=self.s.llm_timeout_seconds) as client:
                for attempt in (1, 2):
                    async with client.stream('POST', url, headers=headers,
                                             json=payload) as resp:
                        # Same one-shot downgrade as the non-streaming path: an
                        # endpoint that rejects ``thinking`` should cost one
                        # retry, not a permanently degraded answer node.
                        if resp.status_code == 400 and 'thinking' in payload and attempt == 1:
                            body = (await resp.aread()).decode('utf-8', 'replace')[:200]
                            self._thinking_supported = False
                            log_event(logger, 'llm_thinking_rejected', level=logging.WARNING,
                                      purpose=purpose, body=body)
                            payload = {k: v for k, v in payload.items() if k != 'thinking'}
                            continue

                        if resp.status_code >= 400:
                            body = (await resp.aread()).decode('utf-8', 'replace')[:300]
                            self._record_failure()
                            metrics.LLM_CALLS.labels(purpose, str(resp.status_code)).inc()
                            log_event(logger, 'llm_stream_error', level=logging.WARNING,
                                      purpose=purpose, status=resp.status_code, body=body)
                            yield 'error', f'http_{resp.status_code}'
                            return

                        async for line in resp.aiter_lines():
                            if not line or not line.startswith('data:'):
                                continue
                            chunk = line[5:].strip()
                            if chunk == '[DONE]':
                                break
                            try:
                                data = json.loads(chunk)
                            except Exception:  # noqa: BLE001
                                continue
                            usage = data.get('usage') or {}
                            if usage:
                                tokens_in, tokens_out, cache_hit, cache_miss = parse_usage(usage)
                                reasoning_tokens = parse_reasoning_tokens(usage)
                            for choice in data.get('choices') or []:
                                delta = choice.get('delta') or {}
                                if delta.get('reasoning_content'):
                                    reasoning_chars += len(delta['reasoning_content'])
                                content = delta.get('content')
                                if content:
                                    collected.append(content)
                                    yield 'delta', content
                        break

            text = ''.join(collected).strip()
            if not text:
                self._record_failure()
                reason = 'thinking_consumed_budget' if reasoning_chars else 'empty_completion'
                metrics.LLM_CALLS.labels(purpose, reason).inc()
                log_event(logger, 'llm_stream_no_answer', level=logging.WARNING,
                          purpose=purpose, reason=reason, reasoning_chars=reasoning_chars)
                yield 'error', reason
                return

            self._record_success()
            result = LLMResult(
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                reasoning_tokens=reasoning_tokens,
                cost_usd=self._price(tokens_in, tokens_out, cache_hit, cache_miss),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                model=self.s.llm_model,
            )
            metrics.LLM_CALLS.labels(purpose, 'ok').inc()
            metrics.LLM_LATENCY.labels(purpose).observe(result.latency_ms / 1000.0)
            metrics.LLM_TOKENS.labels('in').inc(tokens_in)
            metrics.LLM_TOKENS.labels('out').inc(tokens_out)
            metrics.LLM_TOKENS.labels('in_cached').inc(cache_hit)
            metrics.LLM_COST.inc(result.cost_usd)
            yield 'done', result

        except Exception as exc:  # noqa: BLE001 - degrade, never propagate
            self._record_failure()
            metrics.LLM_CALLS.labels(purpose, 'error').inc()
            log_event(logger, 'llm_stream_exception', level=logging.WARNING,
                      purpose=purpose, error=type(exc).__name__, message=str(exc)[:300])
            yield 'error', type(exc).__name__


def build_llm_client(settings: Settings) -> LLMClient:
    return LLMClient(settings)
