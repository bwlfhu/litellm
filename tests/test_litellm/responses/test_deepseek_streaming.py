import asyncio
import json
import threading

import httpx
import pytest

from litellm.exceptions import MidStreamFallbackError
from litellm.router import Router
from litellm.responses.deepseek_streaming import (
    DeepSeekAnthropicResponsesAsyncStream,
    DeepSeekAnthropicResponsesSSEDecoder,
    DeepSeekAnthropicResponsesSyncStream,
)


async def _lines(events: list[tuple[str, dict]]) -> object:
    for event_name, payload in events:
        yield f"event: {event_name}\n"
        yield f"data: {json.dumps(payload)}\n"
        yield ""


@pytest.mark.asyncio
async def test_deepseek_sse_decoder_emits_reasoning_text_and_tool_events_without_anthropic_iterator():
    decoder = DeepSeekAnthropicResponsesSSEDecoder("deepseek-v4-pro", "resp_1")
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 3}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "thinking"}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "reason"}}),
        ("content_block_start", {"index": 1, "content_block": {"type": "text"}}),
        ("content_block_delta", {"index": 1, "delta": {"type": "text_delta", "text": "answer"}}),
        ("content_block_start", {"index": 2, "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup"}}),
        ("content_block_delta", {"index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        ("message_stop", {}),
    ]

    decoded = [event async for event in decoder.decode(_lines(events))]

    assert decoder.output_started is True
    assert [event["type"] for event in decoded] == [
        "response.output_item.added",
        "response.reasoning_summary_text.delta",
        "response.output_item.added",
        "response.output_text.delta",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.completed",
    ]
    assert decoded[-1]["response"]["status"] == "completed"


@pytest.mark.asyncio
async def test_deepseek_sse_decoder_failed_event_does_not_emit_success():
    decoder = DeepSeekAnthropicResponsesSSEDecoder("deepseek-v4-pro", "resp_2")
    decoded = [
        event
        async for event in decoder.decode(
            _lines([("error", {"type": "upstream", "message": "failed"})])
        )
    ]

    assert [event["type"] for event in decoded] == ["response.failed"]
    assert decoded[0]["response"]["status"] == "failed"


@pytest.mark.asyncio
async def test_deepseek_sse_decoder_ignores_message_stop_after_failed_event():
    decoder = DeepSeekAnthropicResponsesSSEDecoder("deepseek-v4-pro", "resp_4")
    decoded = [
        event
        async for event in decoder.decode(
            _lines(
                [
                    ("error", {"type": "upstream", "message": "failed"}),
                    ("message_stop", {}),
                ]
            )
        )
    ]

    assert [event["type"] for event in decoded] == ["response.failed"]


@pytest.mark.asyncio
async def test_deepseek_async_stream_cancellation_closes_response_and_propagates():
    class DelayedBody(httpx.AsyncByteStream):
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    body = DelayedBody()
    response = httpx.Response(200, request=httpx.Request("POST", "https://provider.invalid"), stream=body)
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_3",
        False,
        httpx.AsyncClient(),
    )
    task = asyncio.create_task(stream.__anext__())
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed is True


@pytest.mark.asyncio
async def test_deepseek_async_stream_first_error_requests_router_fallback():
    class EventBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: error\ndata: {"type":"upstream"}\n\n'

        async def aclose(self):
            return None

    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.invalid"),
        stream=EventBody(),
    )
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_pre_output_error",
        False,
        httpx.AsyncClient(),
        pre_output_fallback_enabled=True,
    )

    with pytest.raises(MidStreamFallbackError) as raised:
        await stream.__anext__()
    assert raised.value.is_pre_first_chunk is True
    assert stream._closed is True


@pytest.mark.asyncio
async def test_deepseek_async_stream_post_output_error_is_terminal_event():
    class EventBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: content_block_start\ndata: {"index":0,"content_block":{"type":"text"}}\n\n'
            yield b'event: error\ndata: {"type":"upstream"}\n\n'

        async def aclose(self):
            return None

    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.invalid"),
        stream=EventBody(),
    )
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_post_output_error",
        False,
        httpx.AsyncClient(),
    )

    assert (await stream.__anext__())["type"] == "response.output_item.added"
    assert (await stream.__anext__())["type"] == "response.failed"
    await stream.aclose()


def test_deepseek_sync_stream_close_waits_for_cancelled_worker_cleanup():
    class DelayedBody(httpx.AsyncByteStream):
        def __init__(self):
            self.started = threading.Event()
            self.closed = False

        async def __aiter__(self):
            self.started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    body = DelayedBody()

    async def create_stream():
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://provider.invalid"),
            stream=body,
        )
        return DeepSeekAnthropicResponsesAsyncStream(
            response,
            "deepseek-v4-pro",
            "resp_sync_cancel",
            True,
            httpx.AsyncClient(),
        )

    stream = DeepSeekAnthropicResponsesSyncStream(create_stream())
    assert body.started.wait(timeout=2)
    stream.close()
    assert body.closed is True
    assert stream._thread.is_alive() is False


@pytest.mark.asyncio
async def test_router_retries_deepseek_stream_before_the_first_output_event():
    class EventBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: error\ndata: {"type":"upstream"}\n\n'

        async def aclose(self):
            return None

    primary = DeepSeekAnthropicResponsesAsyncStream(
        httpx.Response(
            200,
            request=httpx.Request("POST", "https://provider.invalid"),
            stream=EventBody(),
        ),
        "deepseek-v4-pro",
        "resp_primary",
        False,
        httpx.AsyncClient(),
        pre_output_fallback_enabled=True,
    )

    async def fallback_stream():
        yield {"type": "response.completed", "response": {"status": "completed"}}

    class RouterWithFallback(Router):
        def __init__(self):
            super().__init__(model_list=[])
            self.fallback_calls = 0

        async def _ageneric_api_call_with_fallbacks(self, **kwargs):
            return primary

        async def async_function_with_fallbacks_common_utils(self, **kwargs):
            assert kwargs["e"].is_pre_first_chunk is True
            self.fallback_calls += 1
            return fallback_stream()

    router = RouterWithFallback()
    wrapped = await router._aresponses_with_streaming_fallbacks(
        original_function=lambda **kwargs: None,
        model="primary",
        input="question",
        stream=True,
        fallbacks=[{"primary": ["backup"]}],
    )
    events = [event async for event in wrapped]

    assert router.fallback_calls == 1
    assert events == [{"type": "response.completed", "response": {"status": "completed"}}]
