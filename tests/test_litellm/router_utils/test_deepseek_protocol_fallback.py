import importlib

import pytest

from litellm.llms.deepseek.anthropic_protocol import (
    DeepSeekProtocolError,
    DeepSeekProtocolNonFallbackError,
    DeepSeekUpstreamError,
)
from litellm.llms.deepseek.responses_transport import DeepSeekRawResponse
from litellm.router import Router
from litellm.responses.deepseek_accounting import DeepSeekParentAccountingTracker, build_attempt_snapshot
from litellm.responses.deepseek_streaming import DeepSeekAnthropicResponsesSyncStream
from litellm.responses.utils import ResponsesAPIRequestUtils
from litellm.router_protocol import protocol_context_from_kwargs
from litellm.types.llms.openai import ResponsesAPIResponse


async def _raise_protocol(*args, **kwargs):
    raise DeepSeekProtocolError("reasoning_history_missing")


@pytest.mark.asyncio
async def test_deepseek_protocol_error_blocks_async_same_group_and_cross_group_fallback():
    router = Router(model_list=[])
    calls = 0

    async def original(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DeepSeekProtocolError("reasoning_history_missing")

    with pytest.raises(DeepSeekProtocolNonFallbackError) as raised:
        await router.async_function_with_fallbacks(
            original_function=original,
            model="primary",
            fallbacks=["backup"],
            num_retries=2,
        )

    assert raised.value.code == "reasoning_history_missing"
    assert raised.value.fallback_allowed is False
    assert calls == 1


def test_deepseek_protocol_error_blocks_sync_fallback():
    router = Router(model_list=[])

    with pytest.raises(DeepSeekProtocolNonFallbackError):
        router.function_with_fallbacks(
            original_function=_raise_protocol,
            model="primary",
            fallbacks=["backup"],
            num_retries=1,
        )


class _RecordingResponsesLogging:
    def __init__(self) -> None:
        self.stream = False
        self.model_call_details: dict[str, object] = {}
        self.successes: list[object] = []
        self.failures: list[object] = []

    def pre_call(self, **kwargs: object) -> None:
        return None

    async def dispatch_success_handlers(self, response: object) -> None:
        self.successes.append(response)

    def failure_handler(self, error: object, *args: object) -> None:
        self.failures.append(error)

    async def async_failure_handler(self, error: object, *args: object) -> None:
        self.failures.append(error)


@pytest.mark.asyncio
async def test_router_aresponses_keeps_protocol_context_and_aggregates_pre_output_fallback(monkeypatch):
    calls: list[str] = []
    logging_obj = _RecordingResponsesLogging()

    async def fake_aresponses(*, model: str, **kwargs: object) -> ResponsesAPIResponse:
        context = protocol_context_from_kwargs(kwargs)
        assert context is not None
        tracker = kwargs.get("_deepseek_parent_accounting_tracker")
        assert isinstance(tracker, DeepSeekParentAccountingTracker)
        calls.append(context.deployment_id)
        tracker.record_attempt(
            build_attempt_snapshot(
                model=model,
                deployment_id=context.deployment_id,
                usage=(
                    {}
                    if context.deployment_id == "primary-id"
                    else {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2}
                ),
                rates=context.rate_snapshot,
            )
        )
        if context.deployment_id == "primary-id":
            raise DeepSeekUpstreamError("connect_error", None)
        response = ResponsesAPIResponse(
            id="resp_fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        )
        # Simulate the DeepSeek bridge's result-side accounting capability.
        response._hidden_params["deepseek_parent_accounting"] = {"attempt_count": 0}
        return response

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    router = Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {"model": "anthropic/primary"},
                "model_info": {
                    "id": "primary-id",
                    "reasoning_protocol": "deepseek_anthropic",
                    "input_cost_per_token": 0.3,
                    "output_cost_per_token": 0.7,
                    "cache_read_input_cost_per_token": 0.05,
                },
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "anthropic/backup"},
                "model_info": {
                    "id": "backup-id",
                    "reasoning_protocol": "deepseek_anthropic",
                    "input_cost_per_token": 0.1,
                    "output_cost_per_token": 0.2,
                    "cache_read_input_cost_per_token": 0.01,
                },
            },
        ],
        num_retries=0,
    )

    response = await router.aresponses(
        model="primary",
        input="question",
        fallbacks=["backup"],
        litellm_logging_obj=logging_obj,
    )

    summary = response._hidden_params["deepseek_parent_accounting"]
    assert calls == ["primary-id", "backup-id"]
    assert response._hidden_params["response_cost"] == pytest.approx(1.62)
    assert summary["attempt_count"] == 2
    assert summary["attempts"][0]["deployment_id"] == "primary-id"
    assert summary["attempts"][0]["cost"] == 0
    assert summary["attempts"][1]["deployment_id"] == "backup-id"
    assert summary["attempts"][1]["cost"] == pytest.approx(1.62)
    assert logging_obj.model_call_details["response_cost"] == pytest.approx(1.62)
    assert logging_obj.failures == []
    assert logging_obj.successes == [response]


def test_router_responses_falls_back_before_output(monkeypatch):
    calls: list[str] = []

    def fake_responses(*, model: str, **kwargs: object) -> ResponsesAPIResponse:
        context = protocol_context_from_kwargs(kwargs)
        assert context is not None
        calls.append(context.deployment_id)
        if context.deployment_id == "primary-id":
            raise DeepSeekUpstreamError("connect_error", None)
        return ResponsesAPIResponse(
            id="resp_sync_fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    monkeypatch.setattr("litellm.responses", fake_responses)
    router = Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {"model": "anthropic/primary"},
                "model_info": {"id": "primary-id", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "anthropic/backup"},
                "model_info": {"id": "backup-id", "reasoning_protocol": "deepseek_anthropic"},
            },
        ],
        num_retries=0,
    )

    response = router.responses(model="primary", input="question", fallbacks=["backup"])

    assert response.id == "resp_sync_fallback"
    assert calls == ["primary-id", "backup-id"]


def test_router_responses_sync_stream_worker_falls_back_before_output(monkeypatch):
    calls: list[str] = []

    async def failed_stream_start() -> object:
        raise DeepSeekUpstreamError("connect_error", None)

    def fake_responses(*, model: str, **kwargs: object) -> object:
        context = protocol_context_from_kwargs(kwargs)
        assert context is not None
        calls.append(context.deployment_id)
        if context.deployment_id == "primary-id":
            return DeepSeekAnthropicResponsesSyncStream(
                failed_stream_start(),
                model=model,
                pre_output_fallback_enabled=True,
            )
        return ResponsesAPIResponse(
            id="resp_sync_stream_fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )

    monkeypatch.setattr("litellm.responses", fake_responses)
    router = _router_with_deepseek_backup()

    stream = router.responses(model="primary", input="question", stream=True, fallbacks=["backup"])

    assert list(stream)[0].id == "resp_sync_stream_fallback"
    assert calls == ["primary-id", "backup-id"]


def _router_with_deepseek_backup() -> Router:
    return Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {"model": "anthropic/primary"},
                "model_info": {"id": "primary-id", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "anthropic/backup"},
                "model_info": {"id": "backup-id", "reasoning_protocol": "deepseek_anthropic"},
            },
        ],
        num_retries=0,
    )


def _invalid_deepseek_tool_history() -> list[dict[str, object]]:
    return [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}]


@pytest.mark.asyncio
async def test_router_aresponses_does_not_fallback_after_public_protocol_error_mapping():
    router = _router_with_deepseek_backup()

    with pytest.raises(DeepSeekProtocolNonFallbackError):
        await router.aresponses(
            model="primary",
            input=_invalid_deepseek_tool_history(),
            reasoning={"effort": "high"},
            fallbacks=["backup"],
        )

    assert router.total_calls["anthropic/primary"] == 1
    assert router.total_calls["anthropic/backup"] == 0


def test_router_responses_does_not_fallback_after_public_protocol_error_mapping():
    router = _router_with_deepseek_backup()

    with pytest.raises(DeepSeekProtocolNonFallbackError):
        router.responses(
            model="primary",
            input=_invalid_deepseek_tool_history(),
            reasoning={"effort": "high"},
            fallbacks=["backup"],
        )

    assert router.total_calls["anthropic/primary"] == 1
    assert router.total_calls["anthropic/backup"] == 0


def test_router_responses_encodes_deepseek_response_id(monkeypatch):
    async def fake_send(*args: object) -> DeepSeekRawResponse:
        import httpx

        return DeepSeekRawResponse(
            httpx.Response(
                200,
                json={
                    "id": "resp-upstream",
                    "content": [{"type": "text", "text": "answer"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "stop_reason": "end_turn",
                },
            )
        )

    bridge_module = importlib.import_module("litellm.responses.deepseek_anthropic")
    monkeypatch.setattr(bridge_module.DeepSeekResponsesRawTransport, "send", fake_send)
    router = Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/primary",
                    "api_base": "https://test.invalid",
                    "api_key": "test-key",
                },
                "model_info": {"id": "primary-id", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )

    response = router.responses(model="primary", input="question")

    decoded = ResponsesAPIRequestUtils._decode_responses_api_response_id(response.id)
    assert decoded["response_id"] == "resp-upstream"
    assert decoded["model_id"] == "primary-id"
    assert response._hidden_params["custom_llm_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_router_aresponses_cross_provider_success_finalizes_deepseek_parent(
    monkeypatch,
):
    """A native Responses fallback is folded into the DeepSeek parent lifecycle."""
    calls: list[str] = []
    logging_obj = _RecordingResponsesLogging()

    async def fake_aresponses(*, model: str, **kwargs: object) -> ResponsesAPIResponse:
        context = protocol_context_from_kwargs(kwargs)
        calls.append(model)
        if context is not None:
            tracker = kwargs["_deepseek_parent_accounting_tracker"]
            assert isinstance(tracker, DeepSeekParentAccountingTracker)
            tracker.record_attempt(
                build_attempt_snapshot(
                    model=model,
                    deployment_id=context.deployment_id,
                    usage={},
                    rates=context.rate_snapshot,
                )
            )
            raise DeepSeekUpstreamError("connect_error", None)
        return ResponsesAPIResponse(
            id="native-fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    router = Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {"model": "anthropic/primary"},
                "model_info": {"id": "primary-id", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "openai/backup"},
                "model_info": {
                    "id": "backup-id",
                    "input_cost_per_token": 0.1,
                    "output_cost_per_token": 0.2,
                },
            },
        ],
        num_retries=0,
    )

    response = await router.aresponses(
        model="primary",
        input="question",
        fallbacks=["backup"],
        litellm_logging_obj=logging_obj,
    )

    assert calls == ["anthropic/primary", "openai/backup"]
    assert response._hidden_params["deepseek_parent_accounting"]["attempt_count"] == 2
    assert response._hidden_params["response_cost"] == pytest.approx(0.3)
    assert response.usage.input_tokens == 1
    assert response.usage.output_tokens == 1
    assert logging_obj.successes == [response]


def test_router_responses_sync_cross_provider_success_finalizes_deepseek_parent(monkeypatch):
    calls: list[str] = []
    logging_obj = _RecordingResponsesLogging()

    def fake_responses(*, model: str, **kwargs: object) -> ResponsesAPIResponse:
        context = protocol_context_from_kwargs(kwargs)
        calls.append(model)
        if context is not None:
            tracker = kwargs["_deepseek_parent_accounting_tracker"]
            assert isinstance(tracker, DeepSeekParentAccountingTracker)
            tracker.record_attempt(
                build_attempt_snapshot(
                    model=model,
                    deployment_id=context.deployment_id,
                    usage={},
                    rates=context.rate_snapshot,
                )
            )
            raise DeepSeekUpstreamError("connect_error", None)
        return ResponsesAPIResponse(
            id="native-sync-fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr("litellm.responses", fake_responses)
    router = Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {"model": "anthropic/primary"},
                "model_info": {"id": "primary-id", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "openai/backup"},
                "model_info": {
                    "id": "backup-id",
                    "input_cost_per_token": 0.1,
                    "output_cost_per_token": 0.2,
                },
            },
        ],
        num_retries=0,
    )

    response = router.responses(
        model="primary",
        input="question",
        stream=True,
        fallbacks=["backup"],
        litellm_logging_obj=logging_obj,
    )

    assert calls == ["anthropic/primary", "openai/backup"]
    assert response._hidden_params["deepseek_parent_accounting"]["attempt_count"] == 2
    assert response._hidden_params["response_cost"] == pytest.approx(0.3)
    assert response.usage.input_tokens == 1
    assert response.usage.output_tokens == 1
    assert logging_obj.successes == [response]
