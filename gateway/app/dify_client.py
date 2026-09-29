# -*- coding: utf-8 -*-
import json, time
import httpx
from .metrics import DIFY_CALLS, DIFY_LATENCY


class DifyError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DifyClient:
    """Async client for the Dify Service API.

    mock=True serves deterministic canned answers / SSE events so the whole
    Gateway demo can run offline (no Docker, no Dify quota, no network).
    All upstream transport failures (DNS, connection refused, timeout, invalid
    JSON) are normalized to DifyError so callers never leak a raw 500.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 90.0, transport=None, mock: bool = False):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport
        self.mock = mock

    def _headers(self):
        return {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}

    def _mock_answer(self, query: str) -> str:
        return (
            '【离线演示】这是由 Gateway Mock 返回的示例回答（未调用真实 Dify）。'
            f'收到的问题：{query[:80]}。'
            '关闭 DIFY_MOCK 并配置真实 DIFY_API_KEY 后即可接入真实 Dify。'
        )

    def _mock_meta(self):
        return {'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}, 'mock': True}

    async def chat_blocking(self, *, query: str, user: str, conversation_id: str = '', inputs=None) -> dict:
        if self.mock:
            return {
                'answer': self._mock_answer(query),
                'conversation_id': 'mock-conversation-0001',
                'message_id': 'mock-message-0001',
                'metadata': self._mock_meta(),
            }
        payload = {'inputs': inputs or {}, 'query': query, 'response_mode': 'blocking', 'conversation_id': conversation_id, 'user': user}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                r = await client.post(f'{self.base_url}/chat-messages', headers=self._headers(), json=payload)
            status = str(r.status_code)
            DIFY_CALLS.labels('blocking', status).inc()
            if r.status_code >= 400:
                raise DifyError(r.status_code, f'Dify error {r.status_code}: {r.text[:1000]}')
            try:
                return r.json()
            except ValueError as e:
                raise DifyError(502, f'Dify returned invalid JSON: {e}') from e
        except httpx.TimeoutException as e:
            raise DifyError(504, f'Dify timeout: {e}') from e
        except httpx.HTTPError as e:
            raise DifyError(502, f'Dify unreachable: {e}') from e
        finally:
            DIFY_LATENCY.labels('blocking').observe(time.perf_counter() - started)

    async def chat_stream(self, *, query: str, user: str, conversation_id: str = '', inputs=None):
        if self.mock:
            common = {'conversation_id': 'mock-conversation-0001', 'message_id': 'mock-message-0001'}
            events = [
                {'event': 'message', 'answer': self._mock_answer(query), **common},
                {'event': 'message_end', 'answer': '', **common},
            ]
            for ev in events:
                yield 'data: ' + json.dumps(ev, ensure_ascii=False)
            return
        payload = {'inputs': inputs or {}, 'query': query, 'response_mode': 'streaming', 'conversation_id': conversation_id, 'user': user}
        try:
            async with httpx.AsyncClient(timeout=None, transport=self.transport) as client:
                async with client.stream('POST', f'{self.base_url}/chat-messages', headers=self._headers(), json=payload) as r:
                    if r.status_code >= 400:
                        body = await r.aread()
                        raise DifyError(r.status_code, body.decode(errors='replace')[:1000])
                    async for line in r.aiter_lines():
                        if line:
                            yield line
        except DifyError:
            raise
        except httpx.TimeoutException as e:
            raise DifyError(504, f'Dify timeout: {e}') from e
        except httpx.HTTPError as e:
            raise DifyError(502, f'Dify unreachable: {e}') from e

    async def submit_human_input(self, *, form_token: str, user: str, inputs: dict, action: str) -> dict:
        if self.mock:
            return {'status': 'submitted', 'action': action, 'message': 'mock HITL submit accepted', 'mock': True}
        payload = {'inputs': inputs, 'action': action, 'user': user}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                r = await client.post(f'{self.base_url}/form/human_input/{form_token}', headers=self._headers(), json=payload)
        except httpx.TimeoutException as e:
            raise DifyError(504, f'Dify timeout: {e}') from e
        except httpx.HTTPError as e:
            raise DifyError(502, f'Dify unreachable: {e}') from e
        if r.status_code >= 400:
            raise DifyError(r.status_code, f'HITL submit failed: {r.text[:1000]}')
        ct = r.headers.get('content-type', '')
        if 'application/json' not in ct:
            return {'raw': r.text}
        try:
            return r.json()
        except ValueError as e:
            raise DifyError(502, f'HITL returned invalid JSON: {e}') from e