"""
Regression tests for fake-streamed providers routed through `/v1/messages`.

A fake-streaming provider (e.g. Vertex AI Gemma `:predict`) collapses its whole
response into a single `MockResponseIterator` chunk that carries content text AND a
`finish_reason` together. `AnthropicStreamWrapper` previously dropped all content in
this case — `translate_streaming_openai_response_to_anthropic` sees the finish_reason
and emits only a `message_delta`. `_CombinedChunkSplitter` splits such chunks so the
content survives.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (
    AnthropicStreamWrapper,
    _CombinedChunkSplitter,
)
from litellm.llms.base_llm.base_model_iterator import MockResponseIterator
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    Choices,
    Delta,
    Function,
    Message,
    ModelResponse,
    ModelResponseStream,
    PromptTokensDetailsWrapper,
    StreamingChoices,
    Usage,
)


def _build_fake_stream(
    content: str, finish_reason: str = "stop"
) -> MockResponseIterator:
    """Mimic a Vertex Gemma `:predict` fake stream: one collapsed chunk."""
    model_response = ModelResponse()
    model_response.choices = [
        Choices(
            index=0,
            message=Message(role="assistant", content=content),
            finish_reason=finish_reason,
        )
    ]
    model_response.usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    model_response.model = "gemma4"
    return MockResponseIterator(model_response=model_response)


def _collect_async(wrapper: AnthropicStreamWrapper) -> str:
    async def _run() -> str:
        out = []
        async for raw in wrapper.async_anthropic_sse_wrapper():
            out.append(raw.decode() if isinstance(raw, bytes) else raw)
        return "".join(out)

    return asyncio.run(_run())


def _collect_sync(wrapper: AnthropicStreamWrapper) -> str:
    return "".join(raw.decode() if isinstance(raw, bytes) else raw for raw in wrapper.anthropic_sse_wrapper())


def _parse_sse(sse: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for block in sse.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


async def _async_chunks(chunks: list[ModelResponseStream]) -> AsyncIterator[ModelResponseStream]:
    for chunk in chunks:
        yield chunk


def test_fake_stream_content_reaches_anthropic_sse():
    """Content from a collapsed fake-stream chunk must be emitted as a delta."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_build_fake_stream("Hello, the answer is 2."),
        model="gemma4",
    )
    sse = _collect_async(wrapper)

    assert "content_block_delta" in sse
    assert "Hello, the answer is 2." in sse
    assert "message_delta" in sse
    assert "message_stop" in sse


def test_fake_stream_usage_preserved():
    """The finish chunk keeps usage so output_tokens is non-zero."""
    wrapper = AnthropicStreamWrapper(
        completion_stream=_build_fake_stream("Two."),
        model="gemma4",
    )
    sse = _collect_async(wrapper)

    message_delta = next(
        json.loads(line[len("data: ") :])
        for block in sse.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ") and '"message_delta"' in line
    )
    assert message_delta["usage"]["output_tokens"] == 5
    assert message_delta["usage"]["input_tokens"] == 10


def test_delayed_usage_chunk_preserves_cache_tokens():
    usage = Usage(
        prompt_tokens=120,
        completion_tokens=5,
        total_tokens=125,
        prompt_tokens_details=PromptTokensDetailsWrapper(
            cached_tokens=30,
            cache_creation_tokens=20,
        ),
    )
    chunks = [
        ModelResponseStream(
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(content="Two."),
                    finish_reason=None,
                )
            ],
        ),
        ModelResponseStream(
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(),
                    finish_reason="stop",
                )
            ],
        ),
        ModelResponseStream(
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(),
                    finish_reason=None,
                )
            ],
            usage=usage,
        ),
    ]
    wrapper = AnthropicStreamWrapper(completion_stream=iter(chunks), model="gpt-4o")
    events = list(wrapper)

    message_delta = next(
        event for event in events if event.get("type") == "message_delta"
    )

    assert message_delta["usage"]["input_tokens"] == 70
    assert message_delta["usage"]["output_tokens"] == 5
    assert message_delta["usage"]["cache_read_input_tokens"] == 30
    assert message_delta["usage"]["cache_creation_input_tokens"] == 20


def test_splitter_passes_through_non_combined_chunks():
    """A chunk with content but no finish_reason is not split."""
    chunk = ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0, delta=Delta(content="partial"), finish_reason=None
            )
        ]
    )
    chunks = list(_CombinedChunkSplitter(iter([chunk])))
    assert len(chunks) == 1
    assert chunks[0].choices[0].delta.content == "partial"


def test_splitter_splits_combined_chunk_into_content_then_finish():
    """A chunk with both content and finish_reason becomes two chunks."""
    chunk = ModelResponseStream(
        choices=[
            StreamingChoices(index=0, delta=Delta(content="done"), finish_reason="stop")
        ]
    )
    content_chunk, finish_chunk = list(_CombinedChunkSplitter(iter([chunk])))

    assert content_chunk.choices[0].delta.content == "done"
    assert content_chunk.choices[0].finish_reason is None

    assert finish_chunk.choices[0].finish_reason == "stop"
    assert finish_chunk.choices[0].delta.content is None


def test_is_combined_false_when_choices_empty():
    """A metadata-only chunk with no choices is never treated as combined."""
    assert _CombinedChunkSplitter._is_combined(SimpleNamespace(choices=[])) is False


def test_is_combined_false_when_delta_missing():
    """A finish chunk whose choice has no delta is not combined."""
    chunk = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", delta=None)])
    assert _CombinedChunkSplitter._is_combined(chunk) is False


def test_split_clears_reasoning_and_thinking_on_finish_chunk():
    """When the combined delta carries reasoning/thinking, only the content
    chunk keeps them — the finish chunk is cleared."""
    delta = SimpleNamespace(
        content="hi",
        tool_calls=None,
        reasoning_content="some reasoning",
        thinking_blocks=[{"type": "thinking"}],
    )
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)]
    )

    content_chunk, finish_chunk = _CombinedChunkSplitter._split(chunk)

    assert content_chunk.choices[0].delta.reasoning_content == "some reasoning"
    assert content_chunk.choices[0].delta.thinking_blocks == [{"type": "thinking"}]
    assert finish_chunk.choices[0].delta.reasoning_content is None
    assert finish_chunk.choices[0].delta.thinking_blocks is None


def _thinking_delta_chunk(thinking: str) -> ModelResponseStream:
    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(
                    reasoning_content=thinking,
                    thinking_blocks=[{"type": "thinking", "thinking": thinking, "signature": None}],
                    provider_specific_fields={
                        "thinking_blocks": [{"type": "thinking", "thinking": thinking, "signature": None}]
                    },
                ),
                finish_reason=None,
            )
        ],
    )


def _signature_chunk(recap: str, signature: str) -> ModelResponseStream:
    return ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(
                    reasoning_content=recap,
                    thinking_blocks=[{"type": "thinking", "thinking": recap, "signature": signature}],
                    provider_specific_fields={
                        "thinking_blocks": [{"type": "thinking", "thinking": recap, "signature": signature}]
                    },
                ),
                finish_reason=None,
            )
        ],
    )


def test_thinking_then_signature_chunk_does_not_crash_stream():
    """Regression for the /v1/messages streaming crash reported on autoroute.

    Anthropic streams extended thinking as incremental ``thinking_delta`` chunks, then a
    closing chunk that recaps the full accumulated thinking AND carries the signature. The
    adapter used to raise ``ValueError`` on that closing chunk, killing the whole stream. It
    must instead emit a single ``signature_delta`` for the recap chunk and never re-emit the
    recap thinking, so the incremental thinking text is not duplicated.
    """
    chunks = [
        _thinking_delta_chunk("First, "),
        _thinking_delta_chunk("reason."),
        _signature_chunk("First, reason.", "sig-abc"),
        ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(content="Done"), finish_reason=None)],
        ),
        ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="stop")],
            usage=Usage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        ),
    ]

    async def _aiter() -> "AsyncIterator[ModelResponseStream]":
        for chunk in chunks:
            yield chunk

    wrapper = AnthropicStreamWrapper(completion_stream=_aiter(), model="claude-haiku-4-5")
    sse = _collect_async(wrapper)

    signature_deltas = [
        json.loads(line[len("data: ") :])
        for block in sse.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ") and '"signature_delta"' in line
    ]
    assert len(signature_deltas) == 1
    assert signature_deltas[0]["delta"]["signature"] == "sig-abc"

    thinking_text = "".join(
        json.loads(line[len("data: ") :])["delta"]["thinking"]
        for block in sse.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ") and '"thinking_delta"' in line
    )
    assert thinking_text == "First, reason."

    assert "message_stop" in sse
    assert "Done" in sse


@pytest.mark.parametrize("is_async", [False, True])
def test_reasoning_only_chunk_is_suppressed_without_losing_following_text(is_async: bool):
    chunks = [
        ModelResponseStream(
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(reasoning_content="internal reasoning"),
                    finish_reason=None,
                )
            ],
        ),
        ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(content="visible answer"), finish_reason=None)],
        ),
        ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="stop")],
            usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        ),
    ]
    completion_stream = _async_chunks(chunks) if is_async else iter(chunks)
    wrapper = AnthropicStreamWrapper(
        completion_stream=completion_stream,
        model="deepseek-reasoner",
        thinking_disabled=True,
    )

    sse = _collect_async(wrapper) if is_async else _collect_sync(wrapper)
    events = _parse_sse(sse)
    delta_types = [event.get("delta", {}).get("type") for event in events]
    block_types = [event.get("content_block", {}).get("type") for event in events]

    assert "thinking_delta" not in delta_types
    assert "thinking" not in block_types
    assert "".join(event.get("delta", {}).get("text", "") for event in events) == "visible answer"


def test_combined_reasoning_text_and_finish_chunk_keeps_text_when_thinking_is_disabled():
    chunk = ModelResponseStream(
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(content="visible answer", reasoning_content="internal reasoning"),
                finish_reason="stop",
            )
        ],
        usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
    )
    wrapper = AnthropicStreamWrapper(
        completion_stream=iter([chunk]),
        model="deepseek-reasoner",
        thinking_disabled=True,
    )

    events = _parse_sse(_collect_sync(wrapper))

    assert all(event.get("delta", {}).get("type") != "thinking_delta" for event in events)
    assert "".join(event.get("delta", {}).get("text", "") for event in events) == "visible answer"


def test_reasoning_and_tool_call_chunk_keeps_tool_when_thinking_is_disabled():
    tool_call = ChatCompletionDeltaToolCall(
        index=0,
        id="call_123",
        function=Function(name="get_weather", arguments='{"city":"Paris"}'),
        type="function",
    )
    chunks = [
        ModelResponseStream(
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(reasoning_content="internal reasoning", tool_calls=[tool_call]),
                    finish_reason=None,
                )
            ],
        ),
        ModelResponseStream(
            choices=[StreamingChoices(index=0, delta=Delta(), finish_reason="tool_calls")],
            usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        ),
    ]
    wrapper = AnthropicStreamWrapper(
        completion_stream=iter(chunks),
        model="deepseek-reasoner",
        thinking_disabled=True,
    )

    events = _parse_sse(_collect_sync(wrapper))
    block_starts = [event["content_block"] for event in events if event.get("type") == "content_block_start"]

    assert all(event.get("delta", {}).get("type") != "thinking_delta" for event in events)
    assert block_starts == [{"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}}]
