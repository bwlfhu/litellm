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
from litellm.responses.deepseek_accounting import (
    AttemptRateSnapshot,
    DeepSeekParentAccountingTracker,
    build_attempt_snapshot,
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
    event_types = [event["type"] for event in decoded]
    assert event_types[:2] == ["response.created", "response.in_progress"]
    assert "response.reasoning_summary_text.delta" in event_types
    assert "response.output_text.delta" in event_types
    assert "response.function_call_arguments.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert event_types.index("response.output_item.done") < event_types.index("response.completed")
    text_delta = next(event for event in decoded if event["type"] == "response.output_text.delta")
    assert text_delta["item_id"] == "msg_resp_1"
    assert text_delta["content_index"] == 0
    assert decoded[-1]["response"]["status"] == "completed"


@pytest.mark.asyncio
async def test_deepseek_sse_decoder_failed_event_does_not_emit_success():
    decoder = DeepSeekAnthropicResponsesSSEDecoder("deepseek-v4-pro", "resp_2")
    decoded = [event async for event in decoder.decode(_lines([("error", {"type": "upstream", "message": "failed"})]))]

    assert [event["type"] for event in decoded][-1] == "response.failed"
    assert [event["type"] for event in decoded][:2] == ["response.created", "response.in_progress"]
    assert decoded[-1]["response"]["status"] == "failed"


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

    assert [event["type"] for event in decoded][-1] == "response.failed"
    assert "response.completed" not in [event["type"] for event in decoded]


@pytest.mark.asyncio
async def test_deepseek_sse_decoder_eof_emits_one_incomplete_terminal_event():
    decoder = DeepSeekAnthropicResponsesSSEDecoder("deepseek-v4-pro", "resp_eof")
    decoded = [
        event
        async for event in decoder.decode(
            _lines([("content_block_start", {"index": 0, "content_block": {"type": "text"}})])
        )
    ]

    assert [event["type"] for event in decoded][:2] == ["response.created", "response.in_progress"]
    assert [event["type"] for event in decoded][-1] == "response.incomplete"
    assert decoded[-1]["response"]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_deepseek_async_stream_eof_before_output_requests_router_fallback():
    class EmptyBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            if False:
                yield b""

        async def aclose(self):
            return None

    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.invalid"),
        stream=EmptyBody(),
    )
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_eof_fallback",
        False,
        httpx.AsyncClient(),
        pre_output_fallback_enabled=True,
    )

    with pytest.raises(MidStreamFallbackError) as raised:
        await stream.__anext__()
    assert raised.value.is_pre_first_chunk is True
    assert stream._closed is True


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
    terminal_events: list[dict[str, object]] = []

    async def on_terminal(event, output_started):
        terminal_events.append({"event": event, "output_started": output_started})

    response = httpx.Response(200, request=httpx.Request("POST", "https://provider.invalid"), stream=body)
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_3",
        False,
        httpx.AsyncClient(),
        on_terminal=on_terminal,
    )
    task = asyncio.create_task(stream.__anext__())
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed is True
    assert terminal_events[0]["event"]["_local_cancellation"] is True


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
async def test_deepseek_async_stream_read_error_before_output_requests_router_fallback():
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("connection reset")
            yield b""  # pragma: no cover

        async def aclose(self):
            return None

    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.invalid"),
        stream=BrokenBody(),
    )
    stream = DeepSeekAnthropicResponsesAsyncStream(
        response,
        "deepseek-v4-pro",
        "resp_read_error",
        False,
        httpx.AsyncClient(),
        pre_output_fallback_enabled=True,
    )

    with pytest.raises(MidStreamFallbackError) as raised:
        await stream.__anext__()
    assert raised.value.is_pre_first_chunk is True
    assert raised.value.original_exception.category == "stream_read_error"


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

    assert (await stream.__anext__())["type"] == "response.created"
    assert (await stream.__anext__())["type"] == "response.in_progress"
    assert (await stream.__anext__())["type"] == "response.output_item.added"
    assert (await stream.__anext__())["type"] == "response.content_part.added"
    assert (await stream.__anext__())["type"] == "response.failed"
    await stream.aclose()


@pytest.mark.asyncio
async def test_deepseek_async_stream_without_router_fallback_preserves_preoutput_lifecycle():
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
        "resp_preoutput_lifecycle",
        False,
        httpx.AsyncClient(),
        pre_output_fallback_enabled=False,
    )

    events = [event async for event in stream]

    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
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

    stream = DeepSeekAnthropicResponsesSyncStream(create_stream(), model="deepseek-v4-pro")
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


@pytest.mark.asyncio
async def test_router_stream_fallback_failure_finalizes_parent_once():
    class Source:
        def __init__(self):
            self._raised = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._raised:
                raise StopAsyncIteration
            self._raised = True
            raise MidStreamFallbackError(
                message="before output",
                model="deepseek-v4-pro",
                llm_provider="deepseek",
                is_pre_first_chunk=True,
            )

        async def aclose(self):
            return None

    class Logging:
        def __init__(self):
            self.failures = []
            self.async_failures = []
            self.model_call_details = {}

        def failure_handler(self, error, *args):
            self.failures.append(error)

        async def async_failure_handler(self, error, *args):
            self.async_failures.append(error)

    tracker = DeepSeekParentAccountingTracker()
    tracker.record_attempt(
        build_attempt_snapshot(
            model="deepseek-v4-pro",
            deployment_id="primary-id",
            usage={},
            rates=AttemptRateSnapshot(),
        )
    )
    logging_obj = Logging()

    async def failed_fallback():
        raise RuntimeError("fallback unavailable")
        yield  # pragma: no cover

    class RouterWithFailedFallback(Router):
        def __init__(self):
            super().__init__(model_list=[])

        async def async_function_with_fallbacks_common_utils(self, **kwargs):
            return failed_fallback()

    router = RouterWithFailedFallback()
    wrapped = await router._aresponses_streaming_iterator(
        response=Source(),
        initial_kwargs={
            "model": "deepseek-v4-pro",
            "stream": True,
            "input": "question",
            "original_generic_function": lambda **kwargs: None,
            "_deepseek_parent_accounting_tracker": tracker,
            "litellm_logging_obj": logging_obj,
        },
    )

    with pytest.raises(RuntimeError, match="fallback unavailable"):
        async for _ in wrapped:
            pass

    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert tracker.claim_lifecycle() is False


@pytest.mark.asyncio
async def test_router_stream_cancellation_finalizes_parent_failure_once():
    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.CancelledError()

        async def aclose(self):
            return None

    class Logging:
        def __init__(self):
            self.failures = []
            self.async_failures = []
            self.model_call_details = {}

        def failure_handler(self, error, *args):
            self.failures.append(error)

        async def async_failure_handler(self, error, *args):
            self.async_failures.append(error)

    tracker = DeepSeekParentAccountingTracker()
    tracker.record_attempt(
        build_attempt_snapshot(
            model="deepseek-v4-pro",
            deployment_id="primary-id",
            usage={},
            rates=AttemptRateSnapshot(),
        )
    )
    logging_obj = Logging()
    router = Router(model_list=[])
    wrapped = await router._aresponses_streaming_iterator(
        response=Source(),
        initial_kwargs={
            "model": "deepseek-v4-pro",
            "stream": True,
            "_deepseek_parent_accounting_tracker": tracker,
            "litellm_logging_obj": logging_obj,
        },
    )

    with pytest.raises(asyncio.CancelledError):
        async for _ in wrapped:
            pass
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert tracker.claim_lifecycle() is False


@pytest.mark.asyncio
async def test_router_stream_fallback_iterator_failure_continues_to_next_candidate():
    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise MidStreamFallbackError(
                message="before output",
                model="deepseek-v4-pro",
                llm_provider="deepseek",
                is_pre_first_chunk=True,
            )

        async def aclose(self):
            return None

    class FailingFallback:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise MidStreamFallbackError(
                message="backup failed before output",
                model="backup-a",
                llm_provider="deepseek",
                is_pre_first_chunk=True,
            )

        async def aclose(self):
            return None

    class RouterWithTwoFallbacks(Router):
        def __init__(self):
            super().__init__(model_list=[])
            self.calls = []

        async def async_function_with_fallbacks_common_utils(self, **kwargs):
            self.calls.append(tuple(kwargs.get("fallbacks", [])))
            if len(self.calls) == 1:
                return FailingFallback()
            return _events_as_async_iterator([{"type": "response.completed", "response": {"status": "completed"}}])

    async def _events_as_async_iterator(events):
        for event in events:
            yield event

    router = RouterWithTwoFallbacks()
    wrapped = await router._aresponses_streaming_iterator(
        response=Source(),
        initial_kwargs={
            "model": "primary",
            "stream": True,
            "input": "question",
            "fallbacks": ["backup-a", "backup-b"],
        },
    )

    events = [event async for event in wrapped]
    assert events == [{"type": "response.completed", "response": {"status": "completed"}}]
    assert len(router.calls) == 2
    assert router.calls[1] == ("backup-b",)


def test_router_sync_responses_stream_fallback_retries_before_output():
    class Source:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            raise MidStreamFallbackError(
                message="before output",
                model="deepseek-v4-pro",
                llm_provider="deepseek",
                is_pre_first_chunk=True,
            )

        def close(self):
            self.closed = True

    class RouterWithFallback(Router):
        def __init__(self):
            super().__init__(model_list=[])
            self.fallback_calls = 0

        async def async_function_with_fallbacks_common_utils(self, **kwargs):
            self.fallback_calls += 1
            return iter([{"type": "response.completed", "response": {"status": "completed"}}])

    source = Source()
    router = RouterWithFallback()
    wrapped = router._responses_streaming_iterator(
        response=source,
        initial_kwargs={
            "model": "deepseek-v4-pro",
            "stream": True,
            "input": "question",
            "fallbacks": ["backup"],
        },
        original_function=lambda **kwargs: None,
    )

    assert list(wrapped) == [{"type": "response.completed", "response": {"status": "completed"}}]
    assert router.fallback_calls == 1
    assert source.closed is True
