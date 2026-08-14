import asyncio
import json

import httpx
import pytest

from litellm.responses.deepseek_streaming import (
    DeepSeekAnthropicResponsesAsyncStream,
    DeepSeekAnthropicResponsesSSEDecoder,
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
