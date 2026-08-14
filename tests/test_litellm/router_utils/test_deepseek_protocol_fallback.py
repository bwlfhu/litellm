import pytest

from litellm.llms.deepseek.anthropic_protocol import (
    DeepSeekProtocolError,
    DeepSeekProtocolNonFallbackError,
    DeepSeekUpstreamError,
)
from litellm.router import Router
from litellm.responses.deepseek_accounting import DeepSeekParentAccountingTracker, build_attempt_snapshot
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
        return ResponsesAPIResponse(
            id="resp_fallback",
            created_at=0,
            model=model,
            object="response",
            output=[],
            status="completed",
            usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        )

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
