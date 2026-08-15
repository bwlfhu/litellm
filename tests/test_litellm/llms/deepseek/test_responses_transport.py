import asyncio
import json

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
    assert (
        request.body
        == freeze_deepseek_request(
            url=request.url,
            headers=request.headers,
            body=request.body,
        ).body
    )


def test_freeze_deepseek_request_matches_anthropic_handler_json_serialization():
    body = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "question"}]}

    frozen = freeze_deepseek_request(url="https://provider.invalid/v1/messages", headers={}, body=body)

    assert frozen.body == json.dumps(body).encode()


@pytest.mark.asyncio
async def test_deepseek_raw_transport_preserves_redirect_as_single_attempt_failure():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            request=request,
            headers={"location": "https://provider.invalid/redirected", "x-request-id": "redirected"},
            content=b"redirect body",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = await DeepSeekResponsesRawTransport(client).send(
        freeze_deepseek_request(url="https://provider.invalid/v1/messages", headers={}, body=b"{}")
    )
    await client.aclose()

    assert isinstance(result, DeepSeekRawFailure)
    assert result.category == "upstream_http_error"
    assert result.status_code == 307
    assert result.headers["x-request-id"] == "redirected"
    assert result.body == b"redirect body"
    assert len(requests) == 1


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
