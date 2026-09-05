"""
Tests for order-based fallback routing.

When deployments have `order` set in litellm_params, lower order deployments
should be tried first, and higher order deployments should be used as fallbacks
when lower order deployments fail.
"""

from typing import Optional

import pytest

import litellm
from litellm import Router
from litellm.utils import _get_order_filtered_deployments

# ---------------------------------------------------------------------------
# Unit tests for _get_order_filtered_deployments
# ---------------------------------------------------------------------------


class TestGetOrderFilteredDeployments:
    def _make_deployment(self, order: Optional[int], dep_id: str) -> dict:
        params: dict = {"model": "gpt-4o", "api_key": "key"}
        if order is not None:
            params["order"] = order
        return {
            "model_name": "test-model",
            "litellm_params": params,
            "model_info": {"id": dep_id},
        }

    def test_returns_min_order_group(self):
        deps = [
            self._make_deployment(1, "a"),
            self._make_deployment(2, "b"),
            self._make_deployment(1, "c"),
        ]
        result = _get_order_filtered_deployments(deps)
        assert len(result) == 2
        assert all(d["model_info"]["id"] in ("a", "c") for d in result)

    def test_target_order_filters_to_exact_level(self):
        deps = [
            self._make_deployment(1, "a"),
            self._make_deployment(2, "b"),
            self._make_deployment(3, "c"),
        ]
        result = _get_order_filtered_deployments(deps, target_order=2)
        assert len(result) == 1
        assert result[0]["model_info"]["id"] == "b"

    def test_target_order_no_match_returns_all(self):
        deps = [
            self._make_deployment(1, "a"),
            self._make_deployment(2, "b"),
        ]
        result = _get_order_filtered_deployments(deps, target_order=99)
        assert len(result) == 2

    def test_no_order_set_returns_all(self):
        deps = [
            self._make_deployment(None, "a"),
            self._make_deployment(None, "b"),
        ]
        result = _get_order_filtered_deployments(deps)
        assert len(result) == 2

    def test_empty_list(self):
        result = _get_order_filtered_deployments([])
        assert result == []

    def test_single_order_returns_all_with_that_order(self):
        deps = [
            self._make_deployment(1, "a"),
            self._make_deployment(1, "b"),
        ]
        result = _get_order_filtered_deployments(deps)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Integration tests for order-based fallback in Router
# ---------------------------------------------------------------------------


def _deepseek_anthropic_fallback_model_list() -> list[dict]:
    return [
        {
            "model_name": "primary",
            "litellm_params": {
                "model": "anthropic/deepseek-v4-pro",
                "api_key": "test",
                "order": 1,
            },
            "model_info": {
                "id": "deepseek-anthropic",
                "reasoning_protocol": "deepseek_anthropic",
            },
        },
        {
            "model_name": "primary",
            "litellm_params": {
                "model": "deepseek-v4-openai",
                "custom_llm_provider": "custom_openai",
                "api_key": "test",
                "order": 2,
            },
            "model_info": {"id": "custom-openai"},
        },
        {
            "model_name": "primary",
            "litellm_params": {
                "model": "anthropic/claude-order-3",
                "api_key": "test",
                "order": 3,
            },
            "model_info": {"id": "claude-order-3"},
        },
        {
            "model_name": "external",
            "litellm_params": {
                "model": "openai/gpt-external",
                "api_key": "test",
            },
            "model_info": {"id": "gpt-external"},
        },
    ]


def test_router_order_without_pre_call_checks():
    """Order filtering should work even when enable_pre_call_checks=False (default)."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "key",
                    "mock_response": "from order 1",
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "key",
                    "mock_response": "from order 2",
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
        ],
        num_retries=0,
        enable_pre_call_checks=False,
    )

    for _ in range(20):
        response = router.completion(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response._hidden_params["model_id"] == "1"


def test_router_order_no_fallback_when_healthy():
    """When order=1 is healthy, order=2 should never be used."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "key",
                    "mock_response": "from order 1",
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "key",
                    "mock_response": "from order 2",
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
        ],
        num_retries=0,
    )

    for _ in range(50):
        response = router.completion(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert response._hidden_params["model_id"] == "1"


@pytest.mark.asyncio
async def test_router_order_fallback_on_failure():
    """When order=1 fails, order=2 should be tried as fallback."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad-key",
                    "mock_response": Exception("connection error"),
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "good-key",
                    "mock_response": "success from order 2",
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
        ],
        num_retries=0,
    )

    response = await router.acompletion(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response._hidden_params["model_id"] == "2"


@pytest.mark.asyncio
async def test_router_order_fallback_three_levels():
    """When order=1 and order=2 both fail, order=3 should be tried."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail 1"),
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail 2"),
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "good",
                    "mock_response": "success from order 3",
                    "order": 3,
                },
                "model_info": {"id": "3"},
            },
        ],
        num_retries=0,
    )

    response = await router.acompletion(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response._hidden_params["model_id"] == "3"


@pytest.mark.asyncio
async def test_router_order_fallback_then_external_fallback():
    """When all order levels fail, external fallbacks should be tried."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail order 1"),
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail order 2"),
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
            {
                "model_name": "fallback-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "good",
                    "mock_response": "success from external fallback",
                },
                "model_info": {"id": "fallback"},
            },
        ],
        fallbacks=[{"test-model": ["fallback-model"]}],
        num_retries=0,
    )

    response = await router.acompletion(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response._hidden_params["model_id"] == "fallback"


@pytest.mark.asyncio
async def test_router_order_fallback_with_non_standard_fallbacks():
    """Non-standard fallback formats (e.g. fallbacks=["model-name"]) passed
    per-request should still be tried after all order levels are exhausted."""
    router = Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail order 1"),
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "test-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "bad",
                    "mock_response": Exception("fail order 2"),
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
            {
                "model_name": "fallback-model",
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_key": "good",
                    "mock_response": "success from non-standard fallback",
                },
                "model_info": {"id": "fallback"},
            },
        ],
        num_retries=0,
    )

    response = await router.acompletion(
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        fallbacks=["fallback-model"],  # non-standard format, passed per-request
    )
    assert response._hidden_params["model_id"] == "fallback"


def test_anthropic_messages_keeps_non_protocol_deployments_eligible():
    router = Router(model_list=_deepseek_anthropic_fallback_model_list())

    try:
        _, deployments = router._common_checks_available_deployment(
            model="primary",
            request_kwargs={"_litellm_router_call_type": "anthropic_messages"},
        )
    finally:
        router.discard()

    assert isinstance(deployments, list)
    assert {deployment["model_info"]["id"] for deployment in deployments} == {
        "deepseek-anthropic",
        "custom-openai",
        "claude-order-3",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_type",
    [litellm.InternalServerError, litellm.RateLimitError, litellm.APIConnectionError],
    ids=["5xx", "429", "network"],
)
async def test_anthropic_messages_order_fallback_crosses_protocol_boundary(failure_type):
    calls: list[tuple[str, str | None, str | None]] = []

    async def provider(**kwargs):
        protocol_context = kwargs.get("_litellm_deployment_protocol_context")
        calls.append(
            (
                kwargs["model"],
                kwargs.get("custom_llm_provider"),
                getattr(protocol_context, "protocol", None),
            )
        )
        if kwargs["model"] == "anthropic/deepseek-v4-pro":
            raise failure_type(message="primary failed", model=kwargs["model"], llm_provider="anthropic")
        return {"selected_model": kwargs["model"]}

    router = Router(model_list=_deepseek_anthropic_fallback_model_list(), num_retries=0)

    try:
        response = await router._ageneric_api_call_with_fallbacks(
            model="primary",
            original_function=provider,
            _litellm_router_call_type="anthropic_messages",
            messages=[],
        )
    finally:
        router.discard()

    assert response["selected_model"] == "deepseek-v4-openai"
    assert calls == [
        ("anthropic/deepseek-v4-pro", "anthropic", "deepseek_anthropic"),
        ("deepseek-v4-openai", "custom_openai", None),
    ]


@pytest.mark.asyncio
async def test_anthropic_messages_order_and_external_fallbacks_clear_protocol_context():
    calls: list[tuple[str, str | None]] = []

    async def provider(**kwargs):
        protocol_context = kwargs.get("_litellm_deployment_protocol_context")
        calls.append((kwargs["model"], getattr(protocol_context, "protocol", None)))
        if kwargs["model"] == "anthropic/deepseek-v4-pro":
            raise litellm.InternalServerError(
                message="primary failed",
                model=kwargs["model"],
                llm_provider="anthropic",
            )
        if kwargs["model"] == "deepseek-v4-openai":
            raise litellm.RateLimitError(
                message="order 2 failed",
                model=kwargs["model"],
                llm_provider="custom_openai",
            )
        if kwargs["model"] == "anthropic/claude-order-3":
            raise litellm.APIConnectionError(
                message="order 3 failed",
                model=kwargs["model"],
                llm_provider="anthropic",
            )
        return {"selected_model": kwargs["model"]}

    router = Router(
        model_list=_deepseek_anthropic_fallback_model_list(),
        fallbacks=[{"primary": ["external"]}],
        num_retries=0,
    )

    try:
        response = await router._ageneric_api_call_with_fallbacks(
            model="primary",
            original_function=provider,
            _litellm_router_call_type="anthropic_messages",
            messages=[],
        )
    finally:
        router.discard()

    assert response["selected_model"] == "openai/gpt-external"
    assert calls == [
        ("anthropic/deepseek-v4-pro", "deepseek_anthropic"),
        ("deepseek-v4-openai", None),
        ("anthropic/claude-order-3", None),
        ("openai/gpt-external", None),
    ]


@pytest.mark.asyncio
async def test_anthropic_messages_local_error_disables_order_and_external_fallbacks():
    calls: list[str] = []
    local_error = litellm.BadRequestError(
        message="invalid local reasoning history",
        model="anthropic/deepseek-v4-pro",
        llm_provider="anthropic",
    )
    setattr(local_error, "_litellm_disable_fallbacks", True)

    async def provider(**kwargs):
        calls.append(kwargs["model"])
        raise local_error

    router = Router(
        model_list=_deepseek_anthropic_fallback_model_list(),
        fallbacks=[{"primary": ["external"]}],
        num_retries=0,
    )

    try:
        with pytest.raises(litellm.BadRequestError) as error:
            await router._ageneric_api_call_with_fallbacks(
                model="primary",
                original_function=provider,
                _litellm_router_call_type="anthropic_messages",
                messages=[],
            )
    finally:
        router.discard()

    assert error.value is local_error
    assert calls == ["anthropic/deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_router_order_fallback_with_wildcard_model_group():
    """Wildcard model groups should also advance across order levels."""
    router = Router(
        model_list=[
            {
                "model_name": "openai/*",
                "litellm_params": {
                    "model": "openai/*",
                    "api_key": "bad",
                    "mock_response": Exception("fail order 1"),
                    "order": 1,
                },
                "model_info": {"id": "1"},
            },
            {
                "model_name": "openai/*",
                "litellm_params": {
                    "model": "openai/*",
                    "api_key": "good",
                    "mock_response": "success from wildcard order 2",
                    "order": 2,
                },
                "model_info": {"id": "2"},
            },
        ],
        num_retries=0,
    )

    response = await router.acompletion(
        model="openai/gpt-4.1-mini",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response._hidden_params["model_id"] == "2"


def test_check_non_standard_fallback_format():
    from litellm.router_utils.fallback_event_handlers import (
        _check_non_standard_fallback_format,
    )

    # Standard formats
    assert _check_non_standard_fallback_format([{"gpt-3.5-turbo": ["claude-3-haiku"]}]) == False
    assert _check_non_standard_fallback_format([{"model": ["qwen-backup"]}]) == False
    assert _check_non_standard_fallback_format([{"model": ["qwen-backup"], "region": ["us-east-1"]}]) == False

    # Non-standard formats
    assert _check_non_standard_fallback_format([{"model": "qwen-backup"}]) == True
    assert (
        _check_non_standard_fallback_format([{"model": "qwen-backup", "messages": [{"role": "user", "content": "hi"}]}])
        == True
    )
    assert _check_non_standard_fallback_format([{"model": ["qwen-backup"], "api_key": "some-key"}]) == True
