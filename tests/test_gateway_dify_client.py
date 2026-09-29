# -*- coding: utf-8 -*-
import json
import pytest
import httpx
from gateway.app.dify_client import DifyClient, DifyError


@pytest.mark.asyncio
async def test_blocking_chat_success():
    def handler(request):
        assert request.url.path.endswith('/chat-messages')
        return httpx.Response(200, json={'answer': 'ok', 'conversation_id': 'c1', 'message_id': 'm1'})
    c = DifyClient('https://example.test/v1', 'app-test', transport=httpx.MockTransport(handler))
    out = await c.chat_blocking(query='hello', user='u1')
    assert out['answer'] == 'ok'


@pytest.mark.asyncio
async def test_blocking_chat_maps_upstream_error():
    c = DifyClient('https://example.test/v1', 'app-test', transport=httpx.MockTransport(lambda r: httpx.Response(500, text='boom')))
    with pytest.raises(DifyError):
        await c.chat_blocking(query='hello', user='u1')


@pytest.mark.asyncio
async def test_blocking_chat_mock_returns_answer_without_network():
    c = DifyClient('https://example.invalid/v1', '', mock=True)
    out = await c.chat_blocking(query='你好', user='u1')
    assert out['answer'] and '离线演示' in out['answer']
    assert out['conversation_id'] and out['message_id']
    assert out['metadata'].get('mock') is True


@pytest.mark.asyncio
async def test_blocking_chat_connect_error_maps_to_502():
    def boom(request):
        raise httpx.ConnectError('all connection attempts failed')
    c = DifyClient('https://example.invalid/v1', 'k', transport=httpx.MockTransport(boom))
    with pytest.raises(DifyError) as ei:
        await c.chat_blocking(query='hi', user='u1')
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_blocking_chat_timeout_maps_to_504():
    def slow(request):
        raise httpx.ReadTimeout('read timed out')
    c = DifyClient('https://example.invalid/v1', 'k', transport=httpx.MockTransport(slow))
    with pytest.raises(DifyError) as ei:
        await c.chat_blocking(query='hi', user='u1')
    assert ei.value.status_code == 504


@pytest.mark.asyncio
async def test_blocking_chat_invalid_json_maps_to_502():
    c = DifyClient('https://example.test/v1', 'k', transport=httpx.MockTransport(lambda r: httpx.Response(200, text='not-json')))
    with pytest.raises(DifyError) as ei:
        await c.chat_blocking(query='hi', user='u1')
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_mock_yields_sse_events():
    c = DifyClient('https://example.invalid/v1', '', mock=True)
    lines = [line async for line in c.chat_stream(query='hi', user='u1')]
    assert len(lines) == 2
    assert all(line.startswith('data: ') for line in lines)
    ev = json.loads(lines[0][len('data: '):])
    assert ev['event'] == 'message' and ev['answer']


@pytest.mark.asyncio
async def test_stream_connect_error_maps_to_502():
    def boom(request):
        raise httpx.ConnectError('down')
    c = DifyClient('https://example.invalid/v1', 'k', transport=httpx.MockTransport(boom))
    with pytest.raises(DifyError) as ei:
        async for _ in c.chat_stream(query='hi', user='u1'):
            pass
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_hitl_mock_submit():
    c = DifyClient('https://example.invalid/v1', '', mock=True)
    out = await c.submit_human_input(form_token='t', user='u1', inputs={}, action='approve')
    assert out.get('status') == 'submitted'