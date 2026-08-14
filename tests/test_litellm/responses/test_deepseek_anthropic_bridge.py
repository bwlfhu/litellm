import json

import httpx
import pytest

import litellm
from litellm.litellm_core_utils.asyncify import run_async_function
from litellm.llms.deepseek.anthropic_protocol import DeepSeekProtocolError, DeepSeekUpstreamError
from litellm.responses.deepseek_anthropic import DeepSeekAnthropicResponsesBridge
from litellm.responses.deepseek_session import (
    SpendLogDeepSeekResponsesSessionRepository,
    create_deepseek_responses_session,
)
from litellm.router_protocol import _build_deployment_protocol_context


def _context():
    context = _build_deployment_protocol_context(
        {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic", "max_input_tokens": 4096},
        "deployment-a",
        "attempt-a",
    )
    assert context is not None
    return context


def _context_with_suffix_budget(suffix_token_budget: int):
    context = _build_deployment_protocol_context(
        {
            "id": "deployment-a",
            "reasoning_protocol": "deepseek_anthropic",
            "deepseek_reasoning_suffix_token_budget": suffix_token_budget,
        },
        "deployment-a",
        "attempt-a",
    )
    assert context is not None
    return context


def _context_with_rates():
    context = _build_deployment_protocol_context(
        {
            "id": "deployment-a",
            "reasoning_protocol": "deepseek_anthropic",
            "max_input_tokens": 4096,
            "input_cost_per_token": 0.1,
            "output_cost_per_token": 0.2,
            "cache_read_input_cost_per_token": 0.01,
        },
        "deployment-a",
        "attempt-a",
    )
    assert context is not None
    return context


def _unexpected_public_entrypoint(**kwargs):
    raise AssertionError("completion entrypoint must not be used")


class _InMemorySessionRepository:
    def __init__(self):
        self._sessions: dict[str, object] = {}

    async def load(self, response_id: str) -> object | None:
        return self._sessions.get(response_id)

    def stage(self, proxy_server_request: object, response_id: str, messages: tuple[dict[str, object], ...]) -> None:
        del proxy_server_request
        self._sessions[response_id] = create_deepseek_responses_session(response_id, messages)


class _RecordingResponsesLogging:
    def __init__(self):
        self.stream = True
        self.model_call_details: dict[str, object] = {}
        self.pre_calls: list[object] = []
        self.successes: list[object] = []
        self.failures: list[object] = []
        self.async_failures: list[object] = []

    def pre_call(self, *, input: object, api_key: str, additional_args: dict[str, object]) -> None:
        assert api_key == ""
        assert additional_args == {}
        self.pre_calls.append(input)

    async def dispatch_success_handlers(self, response: object) -> None:
        self.successes.append(response)

    def failure_handler(self, error: object, *args: object) -> None:
        self.failures.append(error)

    async def async_failure_handler(self, error: object, *args: object) -> None:
        self.async_failures.append(error)


@pytest.mark.asyncio
async def test_deepseek_responses_async_bridge_sends_one_anthropic_wire_request_without_completion(monkeypatch):
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_ds_1",
                "content": [{"type": "thinking", "thinking": "reason"}, {"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 4, "output_tokens": 3},
                "stop_reason": "end_turn",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(litellm, "completion", _unexpected_public_entrypoint)
    monkeypatch.setattr(litellm, "acompletion", _unexpected_public_entrypoint)
    response = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32, "reasoning": {"effort": "high"}},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    await client.aclose()

    assert len(requests) == 1
    assert requests[0]["messages"] == [{"role": "user", "content": "question"}]
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert getattr(response.output[0], "type", None) == "reasoning"
    assert getattr(response.output[1], "type", None) == "message"


@pytest.mark.asyncio
async def test_deepseek_responses_non_stream_parent_accounting_uses_router_rate_snapshot():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_ds_accounted",
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context_with_rates(),
        litellm_logging_obj=logging_obj,
        model_info={"input_cost_per_token": 999},
        client=client,
    )
    await client.aclose()

    assert len(logging_obj.pre_calls) == 1
    assert response.usage.cost == pytest.approx(1.44)
    assert response._hidden_params["response_cost"] == pytest.approx(1.44)
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(1.44)
    assert logging_obj.model_call_details["combined_usage_object"].prompt_tokens == 12
    assert logging_obj.model_call_details["deepseek_parent_accounting"]["attempt_count"] == 1
    assert logging_obj.successes == [response]
    assert logging_obj.failures == []


def test_deepseek_responses_sync_bridge_uses_same_raw_reconstruction_core():
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={"id": "resp_ds_sync", "content": [{"type": "text", "text": "answer"}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=False,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    run_async_function(client.aclose)

    assert len(requests) == 1
    assert response.id == "resp_ds_sync"
    assert requests[0]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_responses_bridge_preserves_reasoning_function_call_and_output_history():
    requests: list[dict] = []
    session_repository = _InMemorySessionRepository()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) > 1:
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "resp_ds_follow_up",
                    "content": [
                        {"type": "thinking", "thinking": "follow up"},
                        {"type": "text", "text": "done"},
                    ],
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_ds_tool",
                "content": [
                    {"type": "thinking", "thinking": "call tool"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "x"}},
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3},
                "stop_reason": "tool_use",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="find",
        responses_api_request={
            "max_output_tokens": 32,
            "reasoning": {"effort": "high"},
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        },
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    second = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input=[{"type": "function_call_output", "call_id": "call-1", "output": "value"}],
        responses_api_request={"max_output_tokens": 32, "previous_response_id": first.id, "reasoning": {"effort": "high"}},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    await client.aclose()

    assert len(requests) == 2
    assert requests[1]["messages"][1]["content"][0] == {"type": "thinking", "thinking": "call tool"}
    assert requests[1]["messages"][2]["content"][0]["tool_use_id"] == "call-1"
    assert second.previous_response_id == first.id


@pytest.mark.asyncio
async def test_deepseek_responses_effort_none_rejects_complete_tool_history_without_http():
    session_repository = _InMemorySessionRepository()
    session_repository.stage(
        None,
        "resp_existing",
        (
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]},
        ),
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request, json={})))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_mode_conflict"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="next",
            responses_api_request={"previous_response_id": "resp_existing", "reasoning": {"effort": "none"}},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            _deepseek_session_repository=session_repository,
            client=client,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_deepseek_responses_uses_router_suffix_budget_before_http():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_context_exhausted"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input=[
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "reason"}]},
                {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call-1", "output": "value"},
            ],
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context_with_suffix_budget(0),
            client=client,
        )
    await client.aclose()

    assert requests == []


@pytest.mark.asyncio
async def test_deepseek_responses_rejects_unknown_previous_response_without_http():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_unrecoverable"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="next",
            responses_api_request={"max_output_tokens": 32, "previous_response_id": "resp_missing"},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            _deepseek_session_repository=_InMemorySessionRepository(),
            client=client,
        )
    await client.aclose()

    assert requests == []


@pytest.mark.asyncio
async def test_deepseek_responses_only_marks_explicit_protocol_400_as_non_fallback_error():
    requests: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(
            400,
            request=request,
            json={"error": {"code": "invalid_request"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekUpstreamError) as raised:
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            client=client,
        )
    await client.aclose()

    assert raised.value.fallback_allowed is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_deepseek_responses_protocol_integrity_400_is_not_fallback_eligible():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={"error": {"code": "reasoning_history_missing"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_missing"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            client=client,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_deepseek_responses_async_stream_uses_pure_decoder_and_completed_event():
    sse = (
        "event: message_start\n"
        'data: {"message":{"usage":{"input_tokens":2}}}\n\n'
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"thinking"}}\n\n'
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"thinking_delta","thinking":"reason"}}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context(),
        client=client,
    )
    events = [event async for event in stream]
    await client.aclose()

    assert [event["type"] for event in events] == [
        "response.output_item.added",
        "response.reasoning_summary_text.delta",
        "response.completed",
    ]
    assert events[-1]["response"]["status"] == "completed"


@pytest.mark.asyncio
async def test_deepseek_responses_stream_completed_history_is_reconstructed_for_next_turn():
    requests: list[dict] = []
    session_repository = _InMemorySessionRepository()
    sse = (
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"thinking"}}\n\n'
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"thinking_delta","thinking":"reason"}}\n\n'
        "event: content_block_start\n"
        'data: {"index":1,"content_block":{"type":"tool_use","id":"call-1","name":"lookup"}}\n\n'
        "event: content_block_delta\n"
        'data: {"index":1,"delta":{"type":"input_json_delta","partial_json":"{}"}}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, request=request, content=sse)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_second",
                "content": [
                    {"type": "thinking", "thinking": "follow up"},
                    {"type": "text", "text": "done"},
                ],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    events = [event async for event in stream]
    response_id = events[-1]["response"]["id"]
    await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input=[{"type": "function_call_output", "call_id": "call-1", "output": "value"}],
        responses_api_request={"max_output_tokens": 32, "previous_response_id": response_id},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    await client.aclose()

    assert len(requests) == 2
    assert requests[1]["messages"][1]["content"][0] == {"type": "thinking", "thinking": "reason"}
    assert requests[1]["messages"][2]["content"][0]["tool_use_id"] == "call-1"


@pytest.mark.asyncio
async def test_deepseek_responses_stream_records_one_parent_accounting_snapshot():
    session_repository = _InMemorySessionRepository()
    logging_obj = _RecordingResponsesLogging()
    sse = (
        "event: message_start\n"
        'data: {"message":{"usage":{"input_tokens":10}}}\n\n'
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"text"}}\n\n'
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"text_delta","text":"answer"}}\n\n'
        "event: message_delta\n"
        'data: {"usage":{"output_tokens":4,"cache_read_input_tokens":2}}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context_with_rates(),
        _deepseek_session_repository=session_repository,
        litellm_logging_obj=logging_obj,
        model_info={
            "input_cost_per_token": 999,
            "output_cost_per_token": 999,
            "cache_read_input_cost_per_token": 999,
        },
        client=client,
    )
    events = [event async for event in stream]
    await client.aclose()

    response = logging_obj.successes[0]
    assert [event["type"] for event in events][-1] == "response.completed"
    assert len(logging_obj.pre_calls) == 1
    assert len(logging_obj.successes) == 1
    assert logging_obj.failures == []
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 4
    assert response.usage.input_tokens_details.cached_tokens == 2
    assert response.usage.cost == pytest.approx(1.62)
    assert response._hidden_params["response_cost"] == pytest.approx(1.62)
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(1.62)
    assert logging_obj.model_call_details["combined_usage_object"].prompt_tokens == 10
    assert logging_obj.model_call_details["deepseek_parent_accounting"]["attempts"][0]["rates"] == {
        "input_cost_per_token": 0.1,
        "output_cost_per_token": 0.2,
        "cache_read_input_cost_per_token": 0.01,
        "cache_creation_input_cost_per_token": 0.0,
    }
    assert await session_repository.load(events[-1]["response"]["id"]) is not None


@pytest.mark.asyncio
async def test_deepseek_responses_stream_failure_records_parent_failure_without_session():
    session_repository = _InMemorySessionRepository()
    logging_obj = _RecordingResponsesLogging()
    sse = (
        "event: message_start\n"
        'data: {"message":{"usage":{"input_tokens":7}}}\n\n'
        "event: error\n"
        'data: {"type":"upstream"}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context_with_rates(),
        _deepseek_session_repository=session_repository,
        litellm_logging_obj=logging_obj,
        client=client,
    )
    events = [event async for event in stream]
    await client.aclose()

    assert [event["type"] for event in events] == ["response.failed"]
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(0.7)
    assert session_repository._sessions == {}


@pytest.mark.asyncio
async def test_deepseek_responses_stream_incomplete_records_parent_failure_without_session():
    session_repository = _InMemorySessionRepository()
    logging_obj = _RecordingResponsesLogging()
    sse = (
        "event: message_start\n"
        'data: {"message":{"usage":{"input_tokens":7}}}\n\n'
        "event: message_delta\n"
        'data: {"delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":2}}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context_with_rates(),
        _deepseek_session_repository=session_repository,
        litellm_logging_obj=logging_obj,
        client=client,
    )
    events = [event async for event in stream]
    await client.aclose()

    assert [event["type"] for event in events] == ["response.incomplete"]
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(1.1)
    assert session_repository._sessions == {}


@pytest.mark.asyncio
async def test_deepseek_spend_log_session_requires_a_complete_atomic_manifest():
    proxy_server_request = {"body": {}}
    messages = (
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason"},
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
            ],
        },
    )

    async def load_spend_logs(response_id: str) -> list[dict[str, object]]:
        assert response_id == "resp_persisted"
        return [{"request_id": "resp_persisted"}]

    async def load_proxy_request(spend_log: dict[str, object]) -> dict[str, object]:
        assert spend_log["request_id"] == "resp_persisted"
        return proxy_server_request["body"]

    repository = SpendLogDeepSeekResponsesSessionRepository(load_spend_logs, load_proxy_request)
    repository.stage(proxy_server_request, "resp_persisted", messages)

    loaded = await repository.load("resp_persisted")
    assert loaded is not None
    assert loaded.messages == messages
    assert loaded.suffix_manifest is not None
    proxy_server_request["body"]["_deepseek_anthropic_session"]["suffix_manifest"]["digest"] = "bad"
    assert await repository.load("resp_persisted") is None


def test_deepseek_responses_sync_stream_worker_forwards_events():
    sse = b"event: message_stop\ndata: {}\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=sse)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=False,
        stream=True,
        protocol_context=_context(),
        client=client,
    )
    events = list(stream)
    stream.close()
    run_async_function(client.aclose)

    assert events[0]["type"] == "response.completed"
