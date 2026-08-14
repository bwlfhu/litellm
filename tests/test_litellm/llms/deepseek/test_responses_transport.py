import asyncio

import httpx
import pytest

from litellm.llms.deepseek.responses_transport import (
    DeepSeekRawFailure,
    DeepSeekRawResponse,
    DeepSeekResponsesRawTransport,
    freeze_deepseek_request,
)


@pytest.mark.asyncio
async def test_deepseek_raw_transport_sends_frozen_body_once_without_status_raise():
    requests: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(
            400,
            request=request,
            headers={"x-request-id": "test-request"},
            content=b'{"error":{"code":"reasoning_history_missing"}}',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = freeze_deepseek_request(
        url="https://provider.invalid/v1/messages",
        headers={"x-api-key": "redacted"},
        body={"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "r"}]}]},
    )
    result = await DeepSeekResponsesRawTransport(client).send(request)
    await client.aclose()

    assert isinstance(result, DeepSeekRawFailure)
    assert result.status_code == 400
    assert result.headers["x-request-id"] == "test-request"
    assert result.body == b'{"error":{"code":"reasoning_history_missing"}}'
    assert requests == [request.body]
    assert request.body == freeze_deepseek_request(
        url=request.url,
        headers=request.headers,
        body=request.body,
    ).body


@pytest.mark.asyncio
async def test_deepseek_raw_transport_connect_error_is_typed_and_not_retried():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await DeepSeekResponsesRawTransport(client).send(
        freeze_deepseek_request(url="https://provider.invalid/v1/messages", headers={}, body=b"{}")
    )
    await client.aclose()

    assert isinstance(result, DeepSeekRawFailure)
    assert result.category == "connect_error"
    assert attempts == 1


@pytest.mark.asyncio
async def test_deepseek_raw_transport_local_cancel_propagates_after_response_close():
    started_read = asyncio.Event()

    class DelayedStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False

        async def __aiter__(self):
            started_read.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            self.closed = True

    stream = DelayedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, request=request, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        DeepSeekResponsesRawTransport(client).send(
            freeze_deepseek_request(url="https://provider.invalid/v1/messages", headers={}, body=b"{}", stream=True)
        )
    )
    await started_read.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()

    assert stream.closed is True


@pytest.mark.asyncio
async def test_deepseek_raw_transport_success_leaves_stream_for_owner():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await DeepSeekResponsesRawTransport(client).send(
        freeze_deepseek_request(url="https://provider.invalid/v1/messages", headers={}, body=b"{}")
    )

    assert isinstance(result, DeepSeekRawResponse)
    assert result.response.status_code == 200
    await result.response.aclose()
    await client.aclose()
