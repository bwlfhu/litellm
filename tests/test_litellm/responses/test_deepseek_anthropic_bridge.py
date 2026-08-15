import importlib
import json

import httpx
import pytest

import litellm
from litellm.litellm_core_utils.asyncify import run_async_function
from litellm.llms.deepseek.anthropic_protocol import DeepSeekProtocolError, DeepSeekUpstreamError
from litellm.responses.deepseek_anthropic import DeepSeekAnthropicResponsesBridge
from litellm.responses.deepseek_accounting import (
    AttemptRateSnapshot,
    DeepSeekParentAccountingTracker,
    build_attempt_snapshot,
)
from litellm.responses.deepseek_session import (
    DeepSeekResponsesSession,
    ProxyDeepSeekResponsesSessionRepository,
    SpendLogDeepSeekResponsesSessionRepository,
    create_deepseek_responses_session,
)
from litellm.responses.utils import ResponsesAPIRequestUtils
from litellm.router_protocol import DeploymentProtocolContext, DeploymentRateSnapshot, DeploymentReasoningProtocol
from litellm.types.llms.openai import ResponsesAPIResponse


def _context():
    return DeploymentProtocolContext(
        protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
        deployment_id="deployment-a",
        attempt_id="attempt-a",
        suffix_token_budget=4096,
        rate_snapshot=DeploymentRateSnapshot(),
        _provenance=object(),
    )


def _context_with_suffix_budget(suffix_token_budget: int):
    return DeploymentProtocolContext(
        protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
        deployment_id="deployment-a",
        attempt_id="attempt-a",
        suffix_token_budget=suffix_token_budget,
        rate_snapshot=DeploymentRateSnapshot(),
        _provenance=object(),
    )


def _context_with_rates():
    return _context_with_rates_for("deployment-a", "attempt-a", 0.1, 0.2, 0.01)


def _context_with_rates_for(
    deployment_id: str,
    attempt_id: str,
    input_cost: float,
    output_cost: float,
    cache_read_cost: float,
):
    return DeploymentProtocolContext(
        protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
        deployment_id=deployment_id,
        attempt_id=attempt_id,
        suffix_token_budget=4096,
        rate_snapshot=DeploymentRateSnapshot(
            input_cost_per_token=input_cost,
            output_cost_per_token=output_cost,
            cache_read_input_cost_per_token=cache_read_cost,
        ),
        _provenance=object(),
    )


def _unexpected_public_entrypoint(**kwargs):
    raise AssertionError("completion entrypoint must not be used")


class _InMemorySessionRepository:
    def __init__(self):
        self._sessions: dict[str, object] = {}

    async def load(self, response_id: str) -> object | None:
        return self._sessions.get(response_id)

    async def commit(self, session: DeepSeekResponsesSession) -> None:
        self._sessions[session.response_id] = session

    def stage(self, proxy_server_request: object, response_id: str, messages: tuple[dict[str, object], ...]) -> None:
        del proxy_server_request
        self._sessions[response_id] = create_deepseek_responses_session(response_id, messages)


class _ProxySessionTable:
    def __init__(self):
        self.records: dict[str, dict[str, object]] = {}

    async def upsert(self, *, where: dict[str, str], data: dict[str, dict[str, object]]) -> None:
        response_id = where["response_id"]
        existing = self.records.get(response_id)
        self.records[response_id] = dict(data["update"] if existing is not None else data["create"])

    async def find_unique(self, *, where: dict[str, str]) -> dict[str, object] | None:
        return self.records.get(where["response_id"])


class _ProxySessionDatabase:
    def __init__(self, table: _ProxySessionTable):
        self.litellm_deepseekresponsessession = table


class _ProxySessionClient:
    def __init__(self, table: _ProxySessionTable):
        self.db = _ProxySessionDatabase(table)


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
async def test_deepseek_responses_maps_system_sampling_and_tool_controls():
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={"id": "resp_ds_controls", "content": [{"type": "text", "text": "ok"}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={
            "instructions": "be concise",
            "max_output_tokens": 32,
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "temperature": 0.2,
            "top_p": 0.8,
        },
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    await client.aclose()

    body = requests[0]
    assert body["system"] == "be concise"
    assert all(message["role"] != "system" for message in body["messages"])
    assert body["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.8


@pytest.mark.asyncio
async def test_deepseek_responses_rejects_unsupported_input_image_before_send():
    sent = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, request=request, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="unsupported_input_item"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input=[{"type": "input_image", "image_url": "redacted"}],
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            client=client,
        )
    await client.aclose()
    assert sent is False


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


@pytest.mark.asyncio
async def test_router_owned_non_stream_bridge_stamps_accounting_for_parent_finalize():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_router_owned",
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 1},
            },
        )

    tracker = DeepSeekParentAccountingTracker()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context_with_rates_for("router-id", "router-attempt", 0.1, 0.2, 0.03),
        _deepseek_parent_accounting_tracker=tracker,
        _deepseek_parent_accounting_owner=True,
        litellm_logging_obj=logging_obj,
        client=client,
    )
    await client.aclose()

    assert response._hidden_params["deepseek_parent_accounting"]["attempt_count"] == 1
    assert logging_obj.successes == []
    await DeepSeekAnthropicResponsesBridge.finalize_router_success(
        tracker=tracker,
        response=response,
        logging_obj=logging_obj,
    )
    assert logging_obj.successes == [response]


@pytest.mark.asyncio
async def test_router_parent_finalize_uses_failure_lifecycle_for_incomplete_response():
    logging_obj = _RecordingResponsesLogging()
    tracker = DeepSeekParentAccountingTracker()
    tracker.record_attempt(
        build_attempt_snapshot(
            model="deepseek-v4-pro",
            deployment_id="deployment-a",
            usage={"input_tokens": 1},
            rates=AttemptRateSnapshot(),
        )
    )
    response = ResponsesAPIResponse(
        id="resp_incomplete_parent",
        created_at=1,
        model="deepseek-v4-pro",
        object="response",
        output=[],
        status="incomplete",
    )

    await DeepSeekAnthropicResponsesBridge.finalize_router_response(
        tracker=tracker,
        response=response,
        logging_obj=logging_obj,
        is_async=True,
    )

    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1


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
        responses_api_request={
            "max_output_tokens": 32,
            "previous_response_id": first.id,
            "reasoning": {"effort": "high"},
        },
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
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request, json={}))
    )
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
async def test_deepseek_responses_raw_failure_preserves_typed_data_and_finalizes_once():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            headers={"x-upstream-request-id": "upstream-id"},
            content=b'{"error":{"code":"invalid_request"}}',
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
            protocol_context=_context_with_rates(),
            litellm_logging_obj=logging_obj,
            client=client,
        )
    await client.aclose()

    assert raised.value.raw_headers == {"x-upstream-request-id": "upstream-id", "content-length": "36"}
    assert raised.value.raw_body == b'{"error":{"code":"invalid_request"}}'
    assert len(logging_obj.pre_calls) == 1
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.failures[0].raw_headers == {}
    assert logging_obj.failures[0].raw_body == b""
    assert logging_obj.model_call_details["deepseek_parent_accounting"]["attempt_count"] == 1
    assert logging_obj.model_call_details["response_cost"] == 0


@pytest.mark.asyncio
async def test_deepseek_responses_message_only_history_400_is_non_fallback_and_keeps_raw_data():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            headers={"x-upstream-request-id": "history-400"},
            content=b'{"type":"invalid_request_error","message":"missing reasoning_content for tool call"}',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_unrecoverable") as raised:
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

    assert raised.value.fallback_allowed is False
    assert raised.value.raw_headers["x-upstream-request-id"] == "history-400"
    assert b"reasoning_content" in raised.value.raw_body


@pytest.mark.asyncio
async def test_deepseek_responses_malformed_payload_finalizes_failure_accounting():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"not-json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="upstream_response_invalid") as raised:
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context_with_rates(),
            litellm_logging_obj=logging_obj,
            client=client,
        )
    await client.aclose()

    assert raised.value.raw_body == b"not-json"
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["deepseek_parent_accounting"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_deepseek_responses_error_shaped_json_200_finalizes_failure_accounting():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"error": {"type": "upstream_error", "message": "invalid upstream payload"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="upstream_response_invalid"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context_with_rates(),
            litellm_logging_obj=logging_obj,
            client=client,
        )
    await client.aclose()

    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["deepseek_parent_accounting"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_deepseek_responses_incomplete_non_stream_uses_failure_lifecycle():
    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_incomplete",
                "content": [{"type": "text", "text": "partial"}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 2, "output_tokens": 1},
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
        client=client,
    )
    await client.aclose()

    assert response.status == "incomplete"
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1


@pytest.mark.asyncio
async def test_deepseek_responses_session_commit_failure_is_typed_and_not_success():
    class FailingSessionRepository:
        requires_atomic_session = True
        supports_atomic_session = True

        async def load(self, response_id: str) -> None:
            return None

        async def commit(self, session: object) -> None:
            raise RuntimeError("storage unavailable")

    logging_obj = _RecordingResponsesLogging()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_commit_failure",
                "content": [{"type": "thinking", "thinking": "reason"}, {"type": "text", "text": "answer"}],
                "usage": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_persistence_failed"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            _deepseek_session_repository=FailingSessionRepository(),
            litellm_logging_obj=logging_obj,
            client=client,
        )
    await client.aclose()

    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1


@pytest.mark.asyncio
async def test_deepseek_session_manifest_is_attached_to_spend_log_proxy_request():
    repository = _InMemorySessionRepository()
    logging_obj = _RecordingResponsesLogging()
    logging_obj.model_call_details = {"litellm_params": {"proxy_server_request": {"body": {"input": "question"}}}}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_session_logging",
                "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
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
        protocol_context=_context(),
        _deepseek_session_repository=repository,
        litellm_logging_obj=logging_obj,
        client=client,
    )
    await client.aclose()

    stored = await repository.load(response.id)
    persisted_request = logging_obj.model_call_details["litellm_params"]["proxy_server_request"]
    assert stored is not None
    marker = persisted_request["body"]["_deepseek_anthropic_session"]
    assert marker == stored.spend_log_marker()
    assert logging_obj.model_call_details["deepseek_session_record"] == marker
    assert "messages" not in marker
    assert '"thinking": "reason"' not in json.dumps(logging_obj.model_call_details, default=str)


@pytest.mark.asyncio
async def test_deepseek_session_without_atomic_repository_rejects_tool_history():
    logging_obj = _RecordingResponsesLogging()
    logging_obj.model_call_details = {"litellm_params": {"proxy_server_request": {"body": {"input": "question"}}}}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_no_atomic_session",
                "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_history_persistence_unavailable"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            litellm_logging_obj=logging_obj,
            client=client,
        )
    await client.aclose()

    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert "deepseek_session_record" not in logging_obj.model_call_details


@pytest.mark.asyncio
async def test_deepseek_responses_fallback_tracker_aggregates_attempts_once():
    logging_obj = _RecordingResponsesLogging()
    tracker = DeepSeekParentAccountingTracker()

    async def unavailable_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    primary_client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable_handler))
    with pytest.raises(DeepSeekUpstreamError) as primary_error:
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-primary",
            input="question",
            responses_api_request={"max_output_tokens": 32},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context_with_rates_for("primary-id", "primary-attempt", 0.3, 0.7, 0.05),
            _deepseek_parent_accounting_tracker=tracker,
            _deepseek_parent_accounting_owner=True,
            litellm_logging_obj=logging_obj,
            client=primary_client,
        )
    await primary_client.aclose()

    async def backup_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_fallback",
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2},
            },
        )

    backup_client = httpx.AsyncClient(transport=httpx.MockTransport(backup_handler))
    response = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-backup",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context_with_rates_for("backup-id", "backup-attempt", 0.1, 0.2, 0.01),
        _deepseek_parent_accounting_tracker=tracker,
        _deepseek_parent_accounting_owner=True,
        litellm_logging_obj=logging_obj,
        client=backup_client,
    )
    await backup_client.aclose()
    await DeepSeekAnthropicResponsesBridge.finalize_router_success(
        tracker=tracker,
        response=response,
        logging_obj=logging_obj,
    )

    summary = response._hidden_params["deepseek_parent_accounting"]
    assert primary_error.value.category == "connect_error"
    assert response.usage.cost == pytest.approx(1.62)
    assert summary["attempt_count"] == 2
    assert summary["attempts"][0]["model"] == "deepseek-primary"
    assert summary["attempts"][0]["deployment_id"] == "primary-id"
    assert summary["attempts"][0]["cost"] == 0
    assert summary["attempts"][0]["rates"]["cache_read_input_cost_per_token"] == 0.05
    assert summary["attempts"][1]["model"] == "deepseek-backup"
    assert summary["attempts"][1]["deployment_id"] == "backup-id"
    assert summary["attempts"][1]["rates"]["cache_read_input_cost_per_token"] == 0.01
    assert len(logging_obj.pre_calls) == 2
    assert logging_obj.failures == []
    assert logging_obj.successes == [response]


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
        assert json.loads(request.content)["stream"] is True
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

    event_types = [event["type"] for event in events]
    assert event_types[:2] == ["response.created", "response.in_progress"]
    assert "response.reasoning_summary_text.delta" in event_types
    assert "response.reasoning_summary_text.done" in event_types
    assert event_types[-1] == "response.completed"
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
    assert ResponsesAPIRequestUtils._decode_responses_api_response_id(response_id)["model_id"] == "deployment-a"
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
        assert json.loads(request.content)["stream"] is True
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
    response_id = ResponsesAPIRequestUtils._decode_responses_api_response_id(events[-1]["response"]["id"])[
        "response_id"
    ]
    assert await session_repository.load(response_id) is not None


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

    assert events[-1]["type"] == "response.failed"
    assert "response.completed" not in [event["type"] for event in events]
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(0.7)
    assert session_repository._sessions == {}


@pytest.mark.asyncio
async def test_deepseek_responses_stream_read_error_after_output_finalizes_parent_failure():
    logging_obj = _RecordingResponsesLogging()

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"message":{"usage":{"input_tokens":7}}}\n\n'
            yield b'event: content_block_start\ndata: {"index":0,"content_block":{"type":"text"}}\n\n'
            raise httpx.ReadError("connection reset")

        async def aclose(self):
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, stream=BrokenStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context_with_rates(),
        litellm_logging_obj=logging_obj,
        client=client,
    )

    with pytest.raises(DeepSeekUpstreamError, match="stream_read_error"):
        async for _event in stream:
            pass
    await client.aclose()

    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(0.7)


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

    assert events[-1]["type"] == "response.incomplete"
    assert "response.completed" not in [event["type"] for event in events]
    assert logging_obj.successes == []
    assert len(logging_obj.failures) == 1
    assert len(logging_obj.async_failures) == 1
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(1.1)
    assert session_repository._sessions == {}


@pytest.mark.asyncio
async def test_deepseek_spend_log_session_requires_a_complete_atomic_manifest():
    proxy_server_request = {"body": {}}
    atomic_sessions: dict[str, DeepSeekResponsesSession] = {}
    messages = (
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason"},
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
            ],
        },
    )

    async def load_atomic_session(response_id: str) -> DeepSeekResponsesSession | None:
        return atomic_sessions.get(response_id)

    async def commit_atomic_session(session: DeepSeekResponsesSession) -> None:
        atomic_sessions[session.response_id] = session

    repository = SpendLogDeepSeekResponsesSessionRepository(
        load_atomic_session=load_atomic_session,
        commit_atomic_session=commit_atomic_session,
    )
    await repository.commit(create_deepseek_responses_session("resp_persisted", messages))
    marker = repository.stage(proxy_server_request, "resp_persisted", messages)

    loaded = await repository.load("resp_persisted")
    assert loaded is not None
    assert loaded.messages == messages
    assert loaded.suffix_manifest is not None
    assert loaded.durability == "atomic"
    assert "messages" not in marker
    proxy_server_request["body"]["_deepseek_anthropic_session"]["suffix_manifest"]["digest"] = "bad"
    assert await repository.load("resp_persisted") is not None
    atomic_sessions["resp_persisted"] = DeepSeekResponsesSession(
        response_id="resp_persisted",
        messages=messages,
        suffix_manifest={"version": 1, "digest": "bad"},
        durability="atomic",
    )
    assert await repository.load("resp_persisted") is None

    spend_log_only = SpendLogDeepSeekResponsesSessionRepository()
    assert await spend_log_only.load("resp_persisted") is None


@pytest.mark.asyncio
async def test_proxy_deepseek_session_repository_encrypts_and_scopes_atomic_history():
    table = _ProxySessionTable()
    client = _ProxySessionClient(table)

    def key_loader() -> str:
        return "test-session-encryption-key"

    repository = ProxyDeepSeekResponsesSessionRepository(
        prisma_client=client,
        owner_id="hashed-owner-a",
        encryption_key_loader=key_loader,
    )
    messages = (
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private-reasoning-value"},
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]},
    )
    session = create_deepseek_responses_session("resp_persisted", messages, durability="atomic")

    await repository.commit(session)

    stored = table.records["resp_persisted"]
    assert "private-reasoning-value" not in json.dumps(stored)
    assert await repository.load("resp_persisted") == session

    other_owner_repository = ProxyDeepSeekResponsesSessionRepository(
        prisma_client=client,
        owner_id="hashed-owner-b",
        encryption_key_loader=key_loader,
    )
    assert await other_owner_repository.load("resp_persisted") is None
    table.records["resp_persisted"]["owner_id"] = "hashed-owner-b"
    assert await other_owner_repository.load("resp_persisted") is None
    table.records["resp_persisted"]["owner_id"] = "hashed-owner-a"
    assert await repository.load("resp_persisted") == session

    table.records["resp_persisted"]["encrypted_payload"] = {}
    assert await repository.load("resp_persisted") is None


@pytest.mark.asyncio
async def test_deepseek_stream_response_ids_remain_unique_when_the_clock_collides(monkeypatch):
    session_repository = _InMemorySessionRepository()
    sse = (
        "event: content_block_start\n"
        'data: {"index":0,"content_block":{"type":"text"}}\n\n'
        "event: content_block_delta\n"
        'data: {"index":0,"delta":{"type":"text_delta","text":"answer"}}\n\n'
        "event: message_stop\n"
        "data: {}\n\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=sse)

    bridge_module = importlib.import_module("litellm.responses.deepseek_anthropic")
    monkeypatch.setattr(bridge_module.time, "time", lambda: 0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first_stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="first",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    second_stream = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="second",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=True,
        protocol_context=_context(),
        _deepseek_session_repository=session_repository,
        client=client,
    )
    first_events = [event async for event in first_stream]
    second_events = [event async for event in second_stream]
    await client.aclose()

    first_response_id = ResponsesAPIRequestUtils._decode_responses_api_response_id(first_events[-1]["response"]["id"])[
        "response_id"
    ]
    second_response_id = ResponsesAPIRequestUtils._decode_responses_api_response_id(
        second_events[-1]["response"]["id"]
    )["response_id"]
    assert first_response_id != second_response_id
    assert set(session_repository._sessions) == {first_response_id, second_response_id}


def test_deepseek_responses_sync_stream_worker_forwards_events():
    sse = b"event: message_stop\ndata: {}\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
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

    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
