"""Pure Anthropic SSE decoding for DeepSeek Responses compatibility."""

import asyncio
import json
import queue
import threading
from collections.abc import AsyncIterator
from typing import Awaitable, Callable, Mapping, cast

import httpx

from litellm.exceptions import MidStreamFallbackError
from litellm.llms.deepseek.anthropic_protocol import DeepSeekProtocolNonFallbackError
from litellm.llms.deepseek.anthropic_protocol import DeepSeekUpstreamError

DeepSeekStreamTerminalHandler = Callable[[Mapping[str, object], bool], Awaitable[None]]
_PUBLIC_OUTPUT_EVENT_TYPES = frozenset(
    {
        "response.output_item.added",
        "response.reasoning_summary_text.delta",
        "response.output_text.delta",
        "response.function_call_arguments.delta",
    }
)


def _is_public_output_event(event: object) -> bool:
    if not isinstance(event, Mapping):
        return False
    event_type = cast(Mapping[str, object], event).get("type")
    return isinstance(event_type, str) and event_type in _PUBLIC_OUTPUT_EVENT_TYPES


class DeepSeekAnthropicResponsesSSEDecoder:
    def __init__(self, model: str, response_id: str):
        self.model = model
        self.response_id = response_id
        self.output_started = False
        self._blocks: dict[int, str] = {}
        self._reasoning = ""
        self._text = ""
        self._arguments: dict[int, str] = {}
        self._tool_calls: dict[int, dict[str, object]] = {}
        self._usage: dict[str, int] = {}
        self._status = "completed"
        self._terminal_emitted = False
        self._lifecycle_started = False
        self._block_items: dict[int, dict[str, object]] = {}
        self._closed_blocks: set[int] = set()
        self._sequence_number = 0

    def _response(self, status: str) -> dict[str, object]:
        output: list[dict[str, object]] = []
        item_status = "completed" if status == "completed" else "in_progress"
        if self._reasoning:
            output.append(
                {
                    "type": "reasoning",
                    "id": f"rs_{self.response_id}",
                    "summary": [{"type": "summary_text", "text": self._reasoning}],
                    "status": item_status,
                }
            )
        if self._text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{self.response_id}",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self._text, "annotations": []}],
                    "status": item_status,
                }
            )
        for index in sorted(self._tool_calls):
            call = self._tool_calls[index]
            output.append(
                {
                    "type": "function_call",
                    "id": call["id"],
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": self._arguments.get(index, ""),
                    "status": item_status,
                }
            )
        input_tokens = self._usage.get("input_tokens", 0)
        output_tokens = self._usage.get("output_tokens", 0)
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": 0,
            "model": self.model,
            "output": output,
            "status": status,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": self._usage.get("cache_read_input_tokens", 0)},
            },
        }

    def _mark_started(self) -> None:
        self.output_started = True

    def _start_lifecycle(self) -> list[dict[str, object]]:
        if self._lifecycle_started:
            return []
        self._lifecycle_started = True
        response = self._response("in_progress")
        return [
            {"type": "response.created", "response": response},
            {"type": "response.in_progress", "response": dict(response)},
        ]

    def _content_block_start(self, payload: Mapping[str, object]) -> list[dict[str, object]]:
        index = payload.get("index")
        block = payload.get("content_block")
        if not isinstance(index, int) or not isinstance(block, Mapping):
            return []
        block_type = block.get("type")
        if block_type not in {"thinking", "text", "tool_use"}:
            return []
        self._blocks[index] = str(block_type)
        if block_type == "tool_use":
            call_id = block.get("id") if isinstance(block.get("id"), str) else f"call_{index}"
            self._tool_calls[index] = {"id": call_id, "name": block.get("name", "")}
            self._arguments[index] = ""
            event_item = {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": block.get("name", ""),
                "arguments": "",
                "status": "in_progress",
            }
        elif block_type == "thinking":
            event_item = {
                "type": "reasoning",
                "id": f"rs_{self.response_id}",
                "summary": [],
                "status": "in_progress",
            }
        else:
            event_item = {
                "type": "message",
                "id": f"msg_{self.response_id}",
                "role": "assistant",
                "content": [],
                "status": "in_progress",
            }
        self._block_items[index] = event_item
        self._mark_started()
        events: list[dict[str, object]] = [
            {"type": "response.output_item.added", "output_index": index, "item": event_item}
        ]
        if block_type == "text":
            events.append(
                {
                    "type": "response.content_part.added",
                    "item_id": event_item["id"],
                    "output_index": index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }
            )
        return events

    def _content_block_delta(self, payload: Mapping[str, object]) -> list[dict[str, object]]:
        index = payload.get("index")
        delta = payload.get("delta")
        if not isinstance(index, int) or not isinstance(delta, Mapping):
            return []
        delta_type = delta.get("type")
        value = delta.get("thinking") or delta.get("text") or delta.get("partial_json") or ""
        if not isinstance(value, str):
            return []
        block_type = self._blocks.get(index)
        self._mark_started()
        item = self._block_items.get(index, {})
        item_id = item.get("id", f"item_{index}")
        if delta_type == "thinking_delta" or block_type == "thinking":
            self._reasoning += value
            return [
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": item_id,
                    "delta": value,
                    "output_index": index,
                    "summary_index": 0,
                }
            ]
        if delta_type == "text_delta" or block_type == "text":
            self._text += value
            return [
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "delta": value,
                    "output_index": index,
                    "content_index": 0,
                }
            ]
        if delta_type == "input_json_delta" or block_type == "tool_use":
            self._arguments[index] = self._arguments.get(index, "") + value
            return [
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item_id,
                    "delta": value,
                    "output_index": index,
                }
            ]
        return []

    def _content_block_stop(self, payload: Mapping[str, object]) -> list[dict[str, object]]:
        index = payload.get("index")
        if not isinstance(index, int) or index in self._closed_blocks:
            return []
        block_type = self._blocks.get(index)
        item = self._block_items.get(index, {})
        item_id = item.get("id", f"item_{index}")
        self._closed_blocks.add(index)
        self._sequence_number += 1
        if block_type == "thinking":
            return [
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "sequence_number": self._sequence_number,
                    "text": self._reasoning,
                },
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "sequence_number": self._sequence_number,
                    "part": {"type": "summary_text", "text": self._reasoning},
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "sequence_number": self._sequence_number,
                    "item": {
                        **item,
                        "summary": [{"type": "summary_text", "text": self._reasoning}],
                        "status": "completed",
                    },
                },
            ]
        if block_type == "text":
            part = {"type": "output_text", "text": self._text, "annotations": []}
            return [
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "text": self._text,
                },
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": part,
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "sequence_number": self._sequence_number,
                    "item": {**item, "content": [part], "status": "completed"},
                },
            ]
        if block_type == "tool_use":
            arguments = self._arguments.get(index, "")
            return [
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item_id,
                    "output_index": index,
                    "arguments": arguments,
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "sequence_number": self._sequence_number,
                    "item": {**item, "arguments": arguments, "status": "completed"},
                },
            ]
        return []

    def _decode_event(self, event_name: str, payload: Mapping[str, object]) -> list[dict[str, object]]:
        if self._terminal_emitted:
            return []
        if event_name in {"error", "message_error"}:
            self._status = "failed"
            self._terminal_emitted = True
            return [{"type": "response.failed", "response": self._response("failed"), "error": dict(payload)}]
        if event_name == "message_start":
            usage = payload.get("message")
            if isinstance(usage, Mapping) and isinstance(usage.get("usage"), Mapping):
                self._usage = {key: int(value) for key, value in usage["usage"].items() if isinstance(value, int)}
            return []
        if event_name == "content_block_start":
            return self._content_block_start(payload)
        if event_name == "content_block_delta":
            return self._content_block_delta(payload)
        if event_name == "content_block_stop":
            return self._content_block_stop(payload)
        if event_name == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                self._usage.update({key: int(value) for key, value in usage.items() if isinstance(value, int)})
            delta = payload.get("delta")
            if isinstance(delta, Mapping) and delta.get("stop_reason") in {"max_tokens", "length"}:
                self._status = "incomplete"
            return []
        if event_name == "message_stop":
            events: list[dict[str, object]] = []
            for index in sorted(self._blocks):
                events.extend(self._content_block_stop({"index": index}))
            self._terminal_emitted = True
            event_type = "response.incomplete" if self._status == "incomplete" else "response.completed"
            events.append({"type": event_type, "response": self._response(self._status)})
            return events
        return []

    def _flush_sse_event(self, event_name: str, data_lines: list[str]) -> list[dict[str, object]]:
        if not data_lines:
            return []
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return []
        return self._decode_event(event_name, payload) if isinstance(payload, Mapping) else []

    async def decode(self, lines: AsyncIterator[str | bytes]) -> AsyncIterator[dict[str, object]]:
        event_name = "message"
        data_lines: list[str] = []

        def with_lifecycle(events: list[dict[str, object]]) -> list[dict[str, object]]:
            if not events:
                return events
            return self._start_lifecycle() + events

        async for raw_line in lines:
            line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
            if line == "":
                for event in with_lifecycle(self._flush_sse_event(event_name, data_lines)):
                    yield event
                event_name = "message"
                data_lines = []
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        for event in with_lifecycle(self._flush_sse_event(event_name, data_lines)):
            yield event
        # A closed HTTP stream without message_stop is not a successful
        # completion. Emit exactly one typed terminal event so accounting and
        # Router fallback see the same state as an explicit incomplete stop.
        if not self._terminal_emitted:
            for event in self._start_lifecycle():
                yield event
            self._status = "incomplete"
            self._terminal_emitted = True
            yield {"type": "response.incomplete", "response": self._response("incomplete")}


class DeepSeekAnthropicResponsesAsyncStream:
    def __init__(
        self,
        response: httpx.Response,
        model: str,
        response_id: str,
        owns_client: bool,
        client: httpx.AsyncClient,
        on_terminal: DeepSeekStreamTerminalHandler | None = None,
        pre_output_fallback_enabled: bool = False,
    ):
        self._response = response
        self._decoder = DeepSeekAnthropicResponsesSSEDecoder(model, response_id)
        self._owns_client = owns_client
        self._client = client
        self._events = self._decoder.decode(response.aiter_lines())
        self._closed = False
        self._on_terminal = on_terminal
        self._pre_output_fallback_enabled = pre_output_fallback_enabled
        self._terminal_notified = False
        self._lifecycle_buffer: list[dict[str, object]] = []
        self._deferred_event: dict[str, object] | None = None

    def __aiter__(self) -> "DeepSeekAnthropicResponsesAsyncStream":
        return self

    async def _notify_terminal(self, event: Mapping[str, object]) -> None:
        if self._terminal_notified or self._on_terminal is None:
            return
        self._terminal_notified = True
        try:
            await self._on_terminal(event, self._decoder.output_started)
        except DeepSeekProtocolNonFallbackError:
            # A session/protocol integrity failure must reach Router as its
            # typed non-fallback error; swallowing it would turn it into a
            # successful terminal event or an ordinary retryable exception.
            raise
        except Exception:
            # Accounting/logging must not mask the original upstream stream
            # failure or a caller cancellation.
            return

    def _synthetic_failure_event(self, category: str, *, local_cancellation: bool = False) -> dict[str, object]:
        event: dict[str, object] = {
            "type": "response.failed",
            "response": self._decoder._response("failed"),
            "error": {"type": category},
        }
        if local_cancellation:
            event["_local_cancellation"] = True
        return event

    async def __anext__(self) -> dict[str, object]:
        try:
            if self._lifecycle_buffer:
                return self._lifecycle_buffer.pop(0)
            if self._deferred_event is not None:
                event = self._deferred_event
                self._deferred_event = None
            else:
                event = await self._events.__anext__()
            event_type = event.get("type")
            while event_type in {"response.created", "response.in_progress"}:
                self._lifecycle_buffer.append(event)
                event = await self._events.__anext__()
                event_type = event.get("type")
            is_terminal = event.get("type") in {"response.completed", "response.failed", "response.incomplete"}
            response = event.get("response")
            if is_terminal and isinstance(response, Mapping):
                await self._notify_terminal(event)
            if self._lifecycle_buffer:
                if (
                    self._decoder.output_started
                    or event_type not in {"response.failed", "response.incomplete"}
                    or not self._pre_output_fallback_enabled
                ):
                    self._deferred_event = event
                    return self._lifecycle_buffer.pop(0)
                self._lifecycle_buffer.clear()
            if (
                event.get("type") in {"response.failed", "response.incomplete"}
                and not self._decoder.output_started
                and self._pre_output_fallback_enabled
            ):
                raise MidStreamFallbackError(
                    message="DeepSeek Responses stream ended before output",
                    model=self._decoder.model,
                    llm_provider="deepseek",
                    original_exception=DeepSeekUpstreamError("stream_failed", None),
                    is_pre_first_chunk=True,
                )
            return event
        except StopAsyncIteration:
            await self.aclose()
            raise
        except asyncio.CancelledError:
            await self.aclose()
            await self._notify_terminal(self._synthetic_failure_event("local_cancelled", local_cancellation=True))
            raise
        except MidStreamFallbackError:
            await self.aclose()
            raise
        except DeepSeekProtocolNonFallbackError:
            await self.aclose()
            raise
        except Exception as error:
            await self.aclose()
            upstream_error = (
                error if isinstance(error, DeepSeekUpstreamError) else DeepSeekUpstreamError("stream_read_error", None)
            )
            await self._notify_terminal(self._synthetic_failure_event(upstream_error.category))
            if self._pre_output_fallback_enabled and not self._decoder.output_started:
                raise MidStreamFallbackError(
                    message="DeepSeek Responses stream read failed before output",
                    model=self._decoder.model,
                    llm_provider="deepseek",
                    original_exception=upstream_error,
                    is_pre_first_chunk=True,
                ) from error
            raise upstream_error from error

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._response.aclose()
        if self._owns_client:
            await self._client.aclose()


class DeepSeekAnthropicResponsesSyncStream:
    def __init__(self, coroutine, *, model: str, pre_output_fallback_enabled: bool = False):
        self._queue: queue.Queue[object] = queue.Queue()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None
        self._model = model
        self._pre_output_fallback_enabled = pre_output_fallback_enabled
        self._worker_started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(coroutine,), daemon=True)
        self._thread.start()

    def _run(self, coroutine) -> None:
        async def worker() -> None:
            output_started = False
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            self._worker_started.set()
            try:
                stream = await coroutine
                async for event in stream:
                    event_object = cast(object, event)
                    output_started = output_started or _is_public_output_event(event_object)
                    self._queue.put(event_object)
            except DeepSeekUpstreamError as error:
                if self._pre_output_fallback_enabled and not output_started:
                    self._queue.put(
                        MidStreamFallbackError(
                            message="DeepSeek Responses stream failed before output",
                            model=self._model,
                            llm_provider="deepseek",
                            original_exception=error,
                            is_pre_first_chunk=True,
                        )
                    )
                else:
                    self._queue.put(error)
            except BaseException as error:
                self._queue.put(error)
            finally:
                if "stream" in locals():
                    await stream.aclose()
                self._queue.put(StopIteration)

        asyncio.run(worker())

    def __iter__(self) -> "DeepSeekAnthropicResponsesSyncStream":
        return self

    def __next__(self) -> dict[str, object]:
        item = self._queue.get()
        if item is StopIteration:
            raise StopIteration
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker_started.wait()
        if self._loop is not None and self._task is not None:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass
        self._thread.join()


__all__ = [
    "DeepSeekAnthropicResponsesAsyncStream",
    "DeepSeekAnthropicResponsesSSEDecoder",
    "DeepSeekAnthropicResponsesSyncStream",
]
