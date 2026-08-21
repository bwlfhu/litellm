import json
import time
from copy import deepcopy
from functools import reduce
from typing import Final
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import litellm
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.anthropic.experimental_pass_through.messages.handler import anthropic_messages_handler
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.llms.deepseek.messages.transformation import (
    DeepSeekAnthropicMessagesConfig,
    _sanitize_deepseek_messages_stream,
    _validate_deepseek_content_blocks,
    prepare_deepseek_chat_history,
)
from litellm.proxy.pass_through_endpoints import streaming_handler
from litellm.router import Router
from litellm.router_protocol import _build_deployment_protocol_context, _RouterDeploymentProtocolContext
from litellm.types.router import GenericLiteLLMParams
from litellm.utils import ProviderConfigManager


def _assert_single_space_thinking_prefix(content: object) -> None:
    assert isinstance(content, list)
    assert len(content) >= 2
    thinking_block: Final = content[0]
    assert isinstance(thinking_block, dict)
    assert len(thinking_block) == 2
    assert thinking_block.get("type") == "thinking"
    assert thinking_block.get("thinking") == " "


def test_deepseek_provider_uses_anthropic_messages_config():
    config = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="deepseek-v4-pro",
        provider=litellm.LlmProviders.DEEPSEEK,
    )

    assert isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert config.custom_llm_provider == "deepseek"


def test_deepseek_anthropic_messages_config_defaults():
    config = DeepSeekAnthropicMessagesConfig()

    assert config.custom_llm_provider == "deepseek"
    assert config.get_api_base() == "https://api.deepseek.com/anthropic"


def test_anthropic_provider_keeps_default_config_for_deepseek_named_model():
    config = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="deepseek-v4-pro",
        provider=litellm.LlmProviders.ANTHROPIC,
    )

    assert isinstance(config, AnthropicMessagesConfig)
    assert not isinstance(config, DeepSeekAnthropicMessagesConfig)


def test_anthropic_chat_transform_does_not_serialize_router_protocol_context():
    context = _RouterDeploymentProtocolContext(
        protocol="deepseek_anthropic",
        messages_path="v1/messages",
        _owner=object(),
    )
    litellm_params = {"_litellm_deployment_protocol_context": context}
    request = AnthropicConfig().transform_request(
        model="claude-test",
        messages=[{"role": "user", "content": "Hello"}],
        optional_params={
            "max_tokens": 100,
            "_litellm_deployment_protocol_context": context,
        },
        litellm_params=litellm_params,
        headers={},
    )

    assert "_litellm_deployment_protocol_context" not in request
    json.dumps(request)


def test_anthropic_chat_disabled_tool_thinking_strips_reasoning_history():
    context = _build_deployment_protocol_context({"deepseek_anthropic_tool_thinking": "disabled"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
                "reasoning_content": "I should call the weather tool.",
            },
            {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
        ],
        optional_params={"max_tokens": 100, "thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}}
    ]


def test_anthropic_chat_disabled_tool_thinking_leaves_non_tool_reasoning_enabled():
    context = _build_deployment_protocol_context({"deepseek_anthropic_tool_thinking": "disabled"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": "Done",
                "reasoning_content": "Solve the task.",
                "tool_calls": [],
            }
        ],
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Solve the task."},
        {"type": "text", "text": "Done"},
    ]


def test_anthropic_chat_disabled_tool_thinking_detects_inline_tool_history():
    context = _build_deployment_protocol_context({"deepseek_anthropic_tool_thinking": "disabled"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Use the weather tool."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
            }
        ],
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}
    ]


def test_anthropic_chat_explicit_disabled_strips_all_reasoning_without_mutating_history():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "inline reasoning", "signature": "old"},
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "I will use the tool."},
            ],
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
            "thinking_blocks": [{"type": "thinking", "thinking": "sidecar reasoning", "signature": "old"}],
            "reasoning_content": "canonical reasoning",
            "reasoning": "foreign reasoning",
            "reasoning_items": [{"type": "reasoning", "id": "reasoning_123"}],
            "thinking": "foreign thinking",
            "provider_specific_fields": {"reasoning_content": "provider reasoning", "source": "provider"},
        }
    ]
    original_messages = deepcopy(messages)

    with patch.object(litellm, "modify_params", True):
        request = AnthropicConfig().transform_request(
            model="deepseek-v4-pro",
            messages=messages,
            optional_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
            litellm_params={"_litellm_deployment_protocol_context": context},
            headers={},
        )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [
        {"type": "text", "text": "I will use the tool."},
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
    ]
    assert messages == original_messages


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "anthropic/claude-sonnet-4-5"])
def test_anthropic_chat_omitted_thinking_defaults_to_enabled_on_first_turn(model):
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model=model,
        messages=[{"role": "user", "content": "Hello"}],
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}


def test_anthropic_chat_omitted_thinking_backfills_reasoningless_tool_history():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    messages: Final = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        }
    ]
    original_messages: Final = deepcopy(messages)

    request: Final = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=messages,
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"]["type"] == "enabled"
    content: Final = request["messages"][0]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 2
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "call_123"
    assert messages == original_messages


def test_anthropic_chat_omitted_thinking_enables_replayable_reasoning_history():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": "Use the weather tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
                "reasoning_content": "I should call the weather tool.",
            },
        ],
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][1]["content"][:2] == [
        {"type": "thinking", "thinking": "I should call the weather tool."},
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
    ]


def test_anthropic_chat_explicit_disabled_strips_reasoning_from_mixed_history():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {"role": "assistant", "content": "Earlier answer", "reasoning_content": "Earlier reasoning"},
            {"role": "user", "content": "Use the weather tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
        ],
        optional_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [{"type": "text", "text": "Earlier answer"}]
    assert request["messages"][2]["content"] == [
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
    ]


def test_anthropic_chat_enabled_thinking_allows_reasoningless_non_tool_history():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "An ordinary answer"},
        ],
        optional_params={"max_tokens": 100, "thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][1]["content"] == [{"type": "text", "text": "An ordinary answer"}]


def test_anthropic_chat_omits_false_stream_only_for_deepseek_context():
    context = _build_deployment_protocol_context({"reasoning_protocol": "deepseek_anthropic"})
    assert context is not None

    deepseek_request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        optional_params={"max_tokens": 100, "stream": False},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )
    anthropic_request = AnthropicConfig().transform_request(
        model="claude-test",
        messages=[{"role": "user", "content": "Hello"}],
        optional_params={"max_tokens": 100, "stream": False},
        litellm_params={},
        headers={},
    )

    assert "stream" not in deepseek_request
    assert deepseek_request["thinking"] == {"type": "enabled"}
    assert anthropic_request["stream"] is False
    assert "thinking" not in anthropic_request


def test_anthropic_chat_uses_trusted_reasoning_placeholder():
    context = _build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "placeholder"})
    assert context is not None

    request = AnthropicConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
        ],
        optional_params={"max_tokens": 100},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert request["messages"][0]["content"][:2] == [
        {"type": "thinking", "thinking": " "},
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
    ]
    assert request["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio(loop_scope="module")
async def test_router_protocol_context_stops_at_anthropic_messages_boundary():
    router = Router(
        model_list=[
            {
                "model_name": "deepseek-v4-pro",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://provider.example.test",
                    "api_key": "test-key",
                },
                "model_info": {
                    "id": "deepseek-deployment",
                    "reasoning_protocol": "deepseek_anthropic",
                    "deepseek_anthropic_messages_path": "v1/messages",
                    "deepseek_anthropic_missing_reasoning": "placeholder",
                },
            }
        ],
        num_retries=0,
    )

    try:
        with patch(
            "litellm.llms.anthropic.experimental_pass_through.messages.handler.base_llm_http_handler.anthropic_messages_handler",
            return_value={"content": []},
        ) as dispatch:
            await router.aanthropic_messages(
                model="deepseek-v4-pro",
                max_tokens=100,
                messages=[{"role": "user", "content": "Hello"}],
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        router.discard()

    dispatched = dispatch.call_args.kwargs
    assert dispatched["deployment_protocol_context"] is not None
    assert dispatched["deployment_protocol_context"].messages_path == "v1/messages"
    assert dispatched["deployment_protocol_context"].missing_reasoning == "placeholder"
    assert dispatched["custom_llm_provider"] == "deepseek"
    assert dispatched["litellm_params"].custom_llm_provider == "deepseek"
    assert dispatched["logging_obj"].model_call_details["agentic_loop_params"]["custom_llm_provider"] == "anthropic"
    request_body = dispatched["anthropic_messages_provider_config"].transform_anthropic_messages_request(
        model=dispatched["model"],
        messages=dispatched["messages"],
        anthropic_messages_optional_request_params=dispatched["anthropic_messages_optional_request_params"],
        litellm_params=dispatched["litellm_params"],
        headers={},
    )

    assert "_litellm_deployment_protocol_context" not in dispatched["kwargs"]
    assert "_litellm_deployment_protocol_context" not in dispatched["anthropic_messages_optional_request_params"]
    assert "_litellm_deployment_protocol_context" not in dispatched["litellm_params"].model_dump()
    assert "_litellm_deployment_protocol_context" not in dispatched["logging_obj"].litellm_params
    assert "_litellm_deployment_protocol_context" not in dispatched["logging_obj"].model_call_details["litellm_params"]
    assert not any(key.startswith("_deepseek_anthropic") for key in dispatched["litellm_params"].model_dump())
    assert not any(key.startswith("_deepseek_anthropic") for key in dispatched["logging_obj"].litellm_params)
    assert not any(
        key.startswith("_deepseek_anthropic") for key in dispatched["logging_obj"].model_call_details["litellm_params"]
    )
    json.dumps(dispatched["logging_obj"].litellm_params)
    json.dumps(request_body)


@pytest.mark.asyncio(loop_scope="module")
async def test_direct_deepseek_provider_receives_trusted_deployment_options_without_legacy_protocol():
    router = Router(
        model_list=[
            {
                "model_name": "deepseek-v4-pro",
                "litellm_params": {
                    "model": "deepseek/deepseek-v4-pro",
                    "api_base": "https://provider.example.test",
                    "api_key": "test-key",
                },
                "model_info": {
                    "id": "deepseek-deployment",
                    "deepseek_anthropic_messages_path": "v1/messages",
                    "deepseek_anthropic_missing_reasoning": "placeholder",
                },
            }
        ],
        num_retries=0,
    )

    try:
        with patch(
            "litellm.llms.anthropic.experimental_pass_through.messages.handler.base_llm_http_handler.anthropic_messages_handler",
            return_value={"content": []},
        ) as dispatch:
            await router.aanthropic_messages(
                model="deepseek-v4-pro",
                max_tokens=100,
                thinking={"type": "enabled"},
                messages=[
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
                    }
                ],
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        router.discard()

    dispatched = dispatch.call_args.kwargs
    config = dispatched["anthropic_messages_provider_config"]
    request_body = config.transform_anthropic_messages_request(
        model=dispatched["model"],
        messages=dispatched["messages"],
        anthropic_messages_optional_request_params=dispatched["anthropic_messages_optional_request_params"],
        litellm_params=dispatched["litellm_params"],
        headers={},
    )

    assert isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert dispatched["custom_llm_provider"] == "deepseek"
    assert request_body["messages"][0]["content"][0] == {"type": "thinking", "thinking": " "}
    assert (
        config.get_complete_url(
            api_base="https://provider.example.test",
            api_key=None,
            model=dispatched["model"],
            optional_params={},
            litellm_params={},
        )
        == "https://provider.example.test/v1/messages"
    )
    assert "_litellm_deployment_protocol_context" not in json.dumps(request_body)
    assert "_deepseek_anthropic" not in json.dumps(request_body)


def test_untrusted_protocol_context_dict_does_not_select_deepseek_config():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with patch.object(
        handler.base_llm_http_handler, "anthropic_messages_handler", return_value="dispatched"
    ) as dispatch:
        result = anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "Hello"}],
            model="anthropic/claude-test",
            custom_llm_provider="anthropic",
            _litellm_deployment_protocol_context={"protocol": "deepseek_anthropic"},
            _deepseek_anthropic_messages_path="v1/messages",
            _deepseek_anthropic_tool_thinking="disabled",
            _deepseek_anthropic_missing_reasoning="placeholder",
        )

    assert result == "dispatched"
    config = dispatch.call_args.kwargs["anthropic_messages_provider_config"]
    assert isinstance(config, AnthropicMessagesConfig)
    assert not isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert "_deepseek_anthropic_messages_path" not in dispatch.call_args.kwargs["litellm_params"]
    assert "_deepseek_anthropic_tool_thinking" not in dispatch.call_args.kwargs["litellm_params"]
    assert "_deepseek_anthropic_missing_reasoning" not in dispatch.call_args.kwargs["litellm_params"]


def test_untrusted_protocol_context_object_does_not_select_deepseek_config():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with patch.object(
        handler.base_llm_http_handler, "anthropic_messages_handler", return_value="dispatched"
    ) as dispatch:
        result = anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "Hello"}],
            model="anthropic/claude-test",
            custom_llm_provider="anthropic",
            _litellm_deployment_protocol_context=_RouterDeploymentProtocolContext(
                protocol="deepseek_anthropic",
                messages_path="v1/messages",
                _owner=object(),
            ),
        )

    assert result == "dispatched"
    config = dispatch.call_args.kwargs["anthropic_messages_provider_config"]
    assert isinstance(config, AnthropicMessagesConfig)
    assert not isinstance(config, DeepSeekAnthropicMessagesConfig)


def test_direct_deepseek_provider_ignores_untrusted_internal_policy_fields():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with patch.object(
        handler.base_llm_http_handler, "anthropic_messages_handler", return_value="dispatched"
    ) as dispatch:
        result = anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "Hello"}],
            model="deepseek/deepseek-v4-pro",
            custom_llm_provider="deepseek",
            _deepseek_anthropic_messages_path="v1/messages",
            _deepseek_anthropic_tool_thinking="disabled",
            _deepseek_anthropic_missing_reasoning="placeholder",
        )

    assert result == "dispatched"
    config = dispatch.call_args.kwargs["anthropic_messages_provider_config"]
    assert isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert (
        config.get_complete_url(
            api_base="https://provider.example.test",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://provider.example.test/anthropic/v1/messages"
    )
    assert not any(
        key.startswith("_deepseek_anthropic") for key in dispatch.call_args.kwargs["litellm_params"].model_dump()
    )


def test_direct_deepseek_prefix_skips_anthropic_history_sanitizer_before_provider_resolution():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with (
        patch.object(handler, "_sanitize_anthropic_tool_history_with_diagnostics") as sanitize,
        patch.object(
            handler.base_llm_http_handler, "anthropic_messages_handler", return_value="dispatched"
        ) as dispatch,
    ):
        result = anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "Hello"}],
            model="deepseek/deepseek-v4-pro",
        )

    assert result == "dispatched"
    sanitize.assert_not_called()
    assert dispatch.call_args.kwargs["custom_llm_provider"] == "deepseek"
    assert isinstance(
        dispatch.call_args.kwargs["anthropic_messages_provider_config"],
        DeepSeekAnthropicMessagesConfig,
    )


def test_deepseek_anthropic_messages_url_defaults_to_anthropic_endpoint():
    config = DeepSeekAnthropicMessagesConfig()

    assert (
        config.get_complete_url(
            api_base=None,
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        config.get_complete_url(
            api_base="https://api.deepseek.com/anthropic/v1",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        config.get_complete_url(
            api_base="https://api.deepseek.com/anthropic",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        config.get_complete_url(
            api_base="https://api.deepseek.com",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        config.get_complete_url(
            api_base="https://api.deepseek.com/v1",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        config.get_complete_url(
            api_base="https://api.deepseek.com/v1/messages",
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == "https://api.deepseek.com/v1/messages"
    )


@pytest.mark.parametrize(
    ("api_base", "messages_path", "expected_url"),
    [
        ("https://api.deepseek.com", None, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic", None, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic/anthropic", None, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/v1/messages", None, "https://api.deepseek.com/v1/messages"),
        (
            "https://api.deepseek.com/anthropic",
            "anthropic/v1/messages",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com/anthropic",
            "v1/messages",
            "https://api.deepseek.com/v1/messages",
        ),
        (
            "https://api.deepseek.com/anthropic/v1/messages",
            "anthropic/v1/messages",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com/anthropic/v1/messages",
            "v1/messages",
            "https://api.deepseek.com/v1/messages",
        ),
        (
            "https://api.deepseek.com/v1/messages",
            "anthropic/v1/messages",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com/v1/messages",
            "v1/messages",
            "https://api.deepseek.com/v1/messages",
        ),
        (
            "https://api.deepseek.com/anthropic/v1",
            "anthropic/v1/messages",
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com",
            "v1/messages",
            "https://api.deepseek.com/v1/messages",
        ),
        (
            "https://api.deepseek.com/v1",
            "v1/messages",
            "https://api.deepseek.com/v1/messages",
        ),
    ],
)
def test_deepseek_anthropic_messages_url_matrix(api_base, messages_path, expected_url):
    assert (
        DeepSeekAnthropicMessagesConfig(messages_path=messages_path).get_complete_url(
            api_base=api_base,
            api_key=None,
            model="deepseek-v4-pro",
            optional_params={},
            litellm_params={},
        )
        == expected_url
    )


def test_deepseek_anthropic_messages_headers_use_deepseek_key():
    config = DeepSeekAnthropicMessagesConfig()

    headers, api_base = config.validate_anthropic_messages_environment(
        headers={},
        model="deepseek-v4-pro",
        messages=[],
        optional_params={},
        litellm_params={},
        api_key="sk-deepseek",
        api_base="https://example.test/anthropic",
    )

    assert api_base == "https://example.test/anthropic"
    assert headers["x-api-key"] == "sk-deepseek"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"


def test_deepseek_anthropic_messages_preserves_thinking_and_sanitizes_custom_tools():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {
            "role": "user",
            "content": "Use the tool.",
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should call the tool.",
                    "signature": "sig",
                },
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "get_weather",
                    "input": {"city": "Sao Paulo"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": "Sunny",
                }
            ],
        },
    ]
    original_messages = deepcopy(messages)

    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "high"},
            "tools": [
                {
                    "type": "custom",
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {"type": "object"},
                },
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": 1,
                },
            ],
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"] == [
        messages[0],
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should call the tool.",
                },
                messages[1]["content"][1],
            ],
        },
        messages[2],
    ]
    assert request["thinking"] == {"type": "enabled"}
    assert request["output_config"] == {"effort": "high"}
    assert request["tools"][0] == {
        "name": "get_weather",
        "description": "Get weather",
        "input_schema": {"type": "object"},
    }
    assert request["tools"][1]["type"] == "web_search_20260209"
    assert messages == original_messages


def test_deepseek_anthropic_messages_backfills_tool_history_without_thinking():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will check the weather."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {"city": "Tokyo"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_123", "content": "Sunny"}],
        },
    ]
    original_messages = deepcopy(messages)

    request: Final = config.transform_anthropic_messages_request(
        model="deepseek-v4-flash",
        messages=messages,
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "high"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"]["type"] == "enabled"
    content: Final = request["messages"][0]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 3
    assert content[1]["text"] == "I will check the weather."
    assert content[2]["id"] == "toolu_123"
    assert content[2]["input"]["city"] == "Tokyo"
    assert request["messages"][1] == messages[1]
    assert messages == original_messages


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("stream", [False, True])
async def test_router_selected_deepseek_messages_backfills_reasoningless_tool_history(stream):
    captured_requests = []

    def mock_transport(request):
        request_body = json.loads(request.content)
        captured_requests.append(request_body)
        if request_body.get("stream") is True:
            return httpx.Response(
                200,
                request=request,
                content=(
                    b'event: message_start\ndata: {"type":"message_start"}\n\n'
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                ),
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_123", "content": "Sunny"}],
        },
    ]

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            response: Final = await router.aanthropic_messages(
                max_tokens=100,
                messages=messages,
                model="deepseek-group",
                stream=stream,
                thinking={"type": "enabled"},
            )
            if stream:
                chunks: Final = [chunk async for chunk in response]
                assert b"".join(chunks) == (
                    b'event: message_start\ndata: {"type":"message_start"}\n\n'
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"]["type"] == "enabled"
    content: Final = captured_requests[0]["messages"][0]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 2
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "toolu_123"


def test_deepseek_anthropic_tool_thinking_policy_leaves_non_tool_request_enabled():
    config = DeepSeekAnthropicMessagesConfig(tool_thinking="disabled")

    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-flash",
        messages=[{"role": "assistant", "content": "Done", "reasoning_content": "Solve the task."}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Solve the task."},
        {"type": "text", "text": "Done"},
    ]


def test_deepseek_anthropic_messages_explicit_disabled_strips_all_reasoning_without_mutating_history():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "inline reasoning", "signature": "old"},
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "I will use the tool."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
            ],
            "thinking_blocks": [{"type": "thinking", "thinking": "sidecar reasoning", "signature": "old"}],
            "reasoning_content": "canonical reasoning",
            "reasoning": "foreign reasoning",
            "reasoning_items": [{"type": "reasoning", "id": "reasoning_123"}],
            "thinking": "foreign thinking",
            "provider_specific_fields": {"reasoning_content": "provider reasoning", "source": "upstream"},
        }
    ]
    original_messages = deepcopy(messages)

    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will use the tool."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
            ],
        }
    ]
    assert messages == original_messages


@pytest.mark.parametrize(
    "message",
    [
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "Reasoning only", "signature": "foreign"}],
        },
        {
            "role": "assistant",
            "content": None,
            "thinking_blocks": [{"type": "thinking", "thinking": "Reasoning only", "signature": "foreign"}],
        },
    ],
)
def test_deepseek_anthropic_messages_rejects_empty_assistant_after_disabled_reasoning_removal(message):
    with pytest.raises(AnthropicError, match="no replayable assistant content") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[message],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize(
    ("request_params", "expects_thinking"),
    [
        ({"max_tokens": 100}, True),
        ({"max_tokens": 100, "thinking": {"type": "enabled"}}, True),
        ({"max_tokens": 100, "thinking": {"type": "disabled"}}, False),
    ],
)
def test_deepseek_anthropic_messages_promotes_reasoning_unless_thinking_is_disabled(request_params, expects_thinking):
    config = DeepSeekAnthropicMessagesConfig()
    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params=request_params,
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )
    logging_obj = MagicMock()
    logging_obj.model_call_details = request

    response = config.transform_anthropic_messages_response(
        model="deepseek-v4-pro",
        raw_response=httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "reasoning_content": " ",
                "provider_specific_fields": {"reasoning_content": "Use the tool.", "source": "deepseek"},
            },
        ),
        logging_obj=logging_obj,
    )

    assert response["content"] == (
        [
            {"type": "thinking", "thinking": "Use the tool."},
            {"type": "text", "text": "Done"},
        ]
        if expects_thinking
        else [{"type": "text", "text": "Done"}]
    )
    assert "provider_specific_fields" not in response


def test_deepseek_anthropic_messages_disabled_response_strips_nonempty_thinking_blocks():
    logging_obj = MagicMock()
    logging_obj.model_call_details = {"thinking": {"type": "disabled"}}

    response = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_response(
        model="deepseek-v4-pro",
        raw_response=httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Upstream reasoning"},
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {"type": "text", "text": "Done"},
                ],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "reasoning_content": "More upstream reasoning",
            },
        ),
        logging_obj=logging_obj,
    )

    assert response["content"] == [{"type": "text", "text": "Done"}]


def test_deepseek_anthropic_messages_response_drops_internal_fields_recursively():
    logging_obj = MagicMock()
    logging_obj.model_call_details = {"thinking": {"type": "enabled"}}

    response = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_response(
        model="deepseek-v4-pro",
        raw_response=httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Use the tool.",
                        "signature": "foreign",
                        "provider_specific_fields": {"source": "foreign"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                        "caller": {"type": "direct"},
                        "thought_signature": "foreign",
                    },
                ],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "reasoning": "foreign",
                "reasoning_items": [{"id": "foreign"}],
                "thinking": "foreign",
                "signature": "foreign",
                "thought_signature": "foreign",
                "provider_specific_fields": {"source": "foreign"},
            },
        ),
        logging_obj=logging_obj,
    )

    assert response["content"] == [
        {"type": "thinking", "thinking": "Use the tool.", "signature": "foreign"},
        {
            "type": "tool_use",
            "id": "toolu_123",
            "name": "get_weather",
            "input": {"city": "Paris"},
            "caller": {"type": "direct"},
        },
    ]
    assert all(
        field not in response
        for field in ("reasoning", "reasoning_items", "thinking", "signature", "thought_signature")
    )
    assert "provider_specific_fields" not in response


def test_deepseek_anthropic_messages_does_not_promote_reasoning_without_request_state():
    logging_obj = MagicMock()
    logging_obj.model_call_details = {}

    response = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_response(
        model="deepseek-v4-pro",
        raw_response=httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "reasoning_content": "Untrusted upstream reasoning",
            },
        ),
        logging_obj=logging_obj,
    )

    assert response["content"] == [{"type": "text", "text": "Done"}]


def test_deepseek_anthropic_messages_normalizes_openai_tool_history_without_mutation():
    messages = [
        {"role": "user", "content": "Use the tool"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Call get_weather.",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps(
                            {
                                "city": "Paris",
                                "reasoning_content": "tool input data",
                                "provider_specific_fields": {"thought_signature": "tool input data"},
                            }
                        ),
                    },
                    "provider_specific_fields": {"thought_signature": "foreign"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": "Sunny",
            "reasoning_content": "foreign",
            "provider_specific_fields": {"thought_signature": "foreign"},
        },
    ]
    original_messages = deepcopy(messages)

    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"] == [
        {"role": "user", "content": "Use the tool"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Call get_weather."},
                {
                    "type": "tool_use",
                    "id": "call_123",
                    "name": "get_weather",
                    "input": {
                        "city": "Paris",
                        "reasoning_content": "tool input data",
                        "provider_specific_fields": {"thought_signature": "tool input data"},
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_123", "content": "Sunny"}],
        },
    ]
    assert messages == original_messages


def test_deepseek_anthropic_messages_normalizes_legacy_function_history():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": "Calling now.",
                "reasoning_content": "Call the function.",
                "function_call": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
            },
            {"role": "function", "name": "get_weather", "content": "Sunny"},
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Call the function."},
                {"type": "text", "text": "Calling now."},
                {
                    "type": "tool_use",
                    "id": "legacy_function_call_0",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "legacy_function_call_0", "content": "Sunny"}],
        },
    ]


def test_deepseek_anthropic_messages_matches_mixed_function_result_by_name():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Choose the modern tool.",
                "function_call": {"name": "legacy_a", "arguments": "{}"},
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "modern_b", "arguments": "{}"},
                    }
                ],
            },
            {"role": "function", "name": "modern_b", "content": "modern result"},
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_b", "content": "modern result"}],
    }


def test_deepseek_anthropic_messages_preserves_tool_ids_during_native_normalization():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Use the tool.",
                "tool_calls": [
                    {
                        "id": "functions.Bash:0",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "functions.Bash:0", "content": "Done"},
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"][1]["id"] == "functions.Bash:0"
    assert request["messages"][1]["content"][0]["tool_use_id"] == "functions.Bash:0"


def test_deepseek_anthropic_messages_rejects_tool_result_without_explicit_id():
    with pytest.raises(AnthropicError, match="tool result is missing tool_call_id"):
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "first", "arguments": "{}"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "second", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "content": "ambiguous"},
            ],
            anthropic_messages_optional_request_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )


@pytest.mark.parametrize(
    "tool_call",
    [
        {"id": "call_1", "type": "function"},
        {"id": "call_1", "type": "function", "function": {"arguments": "{}"}},
        {"id": 1, "type": "function", "function": {"name": "first", "arguments": "{}"}},
        {"id": "call_1", "type": "function", "function": {"name": "first", "arguments": {}}},
    ],
)
def test_deepseek_anthropic_messages_rejects_invalid_tool_calls_locally(tool_call):
    with pytest.raises(AnthropicError, match="tool_calls contain invalid entries"):
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Use the tool.",
                    "tool_calls": [tool_call],
                }
            ],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )


def test_deepseek_anthropic_messages_sanitizes_foreign_history_fields_without_mutation():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Use the tool.", "signature": "foreign"},
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "get_weather",
                    "input": {},
                    "index": 0,
                    "reasoning_content": "Foreign tool reasoning",
                    "provider_specific_fields": {"thought_signature": "foreign"},
                },
            ],
            "provider_specific_fields": {"thought_signature": "foreign"},
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [
                        {"type": "thinking", "thinking": "Foreign nested reasoning"},
                        {
                            "type": "text",
                            "text": "Sunny",
                            "provider_specific_fields": {"thought_signature": "foreign"},
                        },
                    ],
                    "reasoning": "Foreign result reasoning",
                    "provider_specific_fields": {"thought_signature": "foreign"},
                }
            ],
            "reasoning_items": [{"type": "reasoning", "id": "reasoning_123"}],
            "thinking_blocks": [{"type": "thinking", "thinking": "Foreign reasoning"}],
            "provider_specific_fields": {"thought_signature": "foreign"},
        },
    ]
    original_messages = deepcopy(messages)

    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"] == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Use the tool."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [{"type": "text", "text": "Sunny"}],
                }
            ],
        },
    ]
    assert messages == original_messages


@pytest.mark.parametrize("thinking_type", ["enabled", "disabled"])
def test_deepseek_anthropic_messages_does_not_promote_tool_text_to_reasoning(thinking_type):
    logging_obj = MagicMock()
    logging_obj.model_call_details = {"thinking": {"type": thinking_type}}

    response = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_response(
        model="deepseek-v4-pro",
        raw_response=httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I should use the tool."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
                "model": "deepseek-v4-pro",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        logging_obj=logging_obj,
    )

    assert response["content"] == [
        {"type": "text", "text": "I should use the tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]


def test_deepseek_anthropic_messages_backfills_reasoningless_tool_history():
    messages: Final = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
        }
    ]
    original_messages: Final = deepcopy(messages)

    request: Final = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"]["type"] == "enabled"
    content: Final = request["messages"][0]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 2
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "toolu_123"
    assert messages == original_messages


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "claude-sonnet-4-5", "router-alias"])
def test_deepseek_anthropic_messages_omitted_thinking_defaults_to_enabled_on_first_turn(model):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model=model,
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize("model", ["deepseek-v3.2", "deepseek/deepseek-reasoner"])
def test_deepseek_anthropic_messages_non_v4_omitted_thinking_remains_opt_in(model):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model=model,
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert "thinking" not in request


@pytest.mark.parametrize("model", ["deepseek-v3.2", "deepseek/deepseek-reasoner"])
def test_deepseek_anthropic_messages_non_v4_explicit_thinking_is_preserved(model):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model=model,
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}


def test_deepseek_anthropic_messages_omitted_thinking_backfills_reasoningless_tool_history():
    request: Final = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"]["type"] == "enabled"
    content: Final = request["messages"][0]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 2
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "toolu_123"


def test_deepseek_anthropic_messages_omitted_thinking_enables_thinking_block_history():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Use the weather tool."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Use the weather tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]


def test_deepseek_anthropic_messages_omitted_thinking_enables_reasoning_content_history():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
                "reasoning_content": "Use the weather tool.",
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Use the weather tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]


def test_deepseek_anthropic_messages_explicit_disabled_strips_reasoning_from_mixed_history():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "An earlier reasoning turn."},
                    {"type": "text", "text": "Done"},
                ],
            },
            {"role": "user", "content": "Use the weather tool"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
            },
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [{"type": "text", "text": "Done"}]
    assert request["messages"][2]["content"] == [
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}
    ]


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_deepseek_anthropic_messages_enabled_thinking_disables_when_non_tool_history_lost_reasoning(model):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model=model,
        messages=[
            {"role": "user", "content": "First turn"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Replayable reasoning"},
                    {"type": "text", "text": "First answer"},
                ],
            },
            {"role": "user", "content": "Next turn"},
            {"role": "assistant", "content": [{"type": "text", "text": "An ordinary answer"}]},
            {"role": "user", "content": "Continue"},
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in request
    assert "output_config" not in request
    assert request["messages"][1]["content"] == [{"type": "text", "text": "First answer"}]
    assert request["messages"][3]["content"] == [{"type": "text", "text": "An ordinary answer"}]


@pytest.mark.parametrize(("stream", "expected_stream"), [(False, None), (True, True)])
def test_deepseek_anthropic_messages_omits_only_false_stream(stream, expected_stream):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "stream": stream,
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request.get("stream") is expected_stream


def test_deepseek_anthropic_messages_placeholder_is_exactly_one_space():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": " \t ", "signature": "old"},
                    {"type": "text", "text": "I will use the tool."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
            }
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": " "},
        {"type": "text", "text": "I will use the tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]
    assert request["thinking"] == {"type": "enabled"}


def test_deepseek_anthropic_messages_prefers_real_sidecar_reasoning_to_placeholder():
    request = DeepSeekAnthropicMessagesConfig(missing_reasoning="placeholder").transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "   ", "signature": "old"},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
                "thinking_blocks": [{"type": "thinking", "thinking": "Use the weather tool.", "signature": "old"}],
            }
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Use the weather tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]
    assert "thinking_blocks" not in request["messages"][0]


@pytest.mark.parametrize("content", [None, ""])
def test_deepseek_anthropic_messages_preserves_scalar_sidecar_reasoning(content):
    messages = [
        {
            "role": "assistant",
            "content": content,
            "thinking_blocks": [{"type": "thinking", "thinking": "Use the weather tool.", "signature": "old"}],
        }
    ]
    original_messages = deepcopy(messages)

    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [{"type": "thinking", "thinking": "Use the weather tool."}]
    assert request["thinking"] == {"type": "enabled"}
    assert messages == original_messages


@pytest.mark.parametrize("content", [None, ""])
def test_deepseek_chat_bridge_preserves_scalar_sidecar_reasoning(content):
    prepared = prepare_deepseek_chat_history(
        [
            {
                "role": "assistant",
                "content": content,
                "thinking_blocks": [{"type": "thinking", "thinking": "Use the weather tool.", "signature": "old"}],
            }
        ]
    )

    assert prepared[0]["content"] == content
    assert prepared[0]["thinking_blocks"] == [{"type": "thinking", "thinking": "Use the weather tool."}]


def test_deepseek_anthropic_messages_prefers_canonical_reasoning_over_stale_blocks():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "stale inline", "signature": "old"},
                    {"type": "text", "text": "Visible answer"},
                ],
                "thinking_blocks": [{"type": "thinking", "thinking": "stale sidecar", "signature": "old"}],
                "reasoning_content": "canonical reasoning",
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "canonical reasoning"},
        {"type": "text", "text": "Visible answer"},
    ]


def test_deepseek_chat_bridge_prefers_canonical_reasoning_over_stale_blocks():
    prepared = prepare_deepseek_chat_history(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "stale inline", "signature": "old"},
                    {"type": "text", "text": "Visible answer"},
                ],
                "thinking_blocks": [{"type": "thinking", "thinking": "stale sidecar", "signature": "old"}],
                "reasoning_content": "canonical reasoning",
            }
        ]
    )

    assert prepared[0]["content"] == [{"type": "text", "text": "Visible answer"}]
    assert prepared[0]["thinking_blocks"] == [{"type": "thinking", "thinking": "canonical reasoning"}]


def test_deepseek_anthropic_messages_drops_redacted_tool_history_when_thinking_is_disabled():
    request = DeepSeekAnthropicMessagesConfig(missing_reasoning="placeholder").transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100, "thinking": {"type": "disabled"}},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}
    ]


def test_deepseek_anthropic_messages_rejects_redacted_tool_history_in_placeholder_mode():
    with pytest.raises(AnthropicError, match="cannot replay redacted thinking") as error:
        DeepSeekAnthropicMessagesConfig(missing_reasoning="placeholder").transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "encrypted"},
                        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                    ],
                }
            ],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "enabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize(
    ("tool_choice", "expected_tool_choice", "expected_thinking"),
    [
        ("auto", {"type": "auto"}, {"type": "enabled"}),
        ("none", {"type": "none"}, {"type": "enabled"}),
        ("required", {"type": "any"}, {"type": "enabled"}),
        (
            {"type": "required", "disable_parallel_tool_use": True},
            {"type": "any", "disable_parallel_tool_use": True},
            {"type": "enabled"},
        ),
        ({"type": "any"}, {"type": "any"}, {"type": "enabled"}),
        ({"type": "tool", "name": "get_weather"}, {"type": "tool", "name": "get_weather"}, {"type": "enabled"}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"type": "tool", "name": "get_weather"},
            {"type": "enabled"},
        ),
    ],
)
def test_deepseek_anthropic_messages_preserves_tool_choice_semantics(
    tool_choice, expected_tool_choice, expected_thinking
):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the weather tool."}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "tool_choice": tool_choice,
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["tool_choice"] == expected_tool_choice
    assert request["thinking"] == expected_thinking


def test_deepseek_anthropic_tool_thinking_policy_preserves_forced_choice_while_disabling_thinking():
    request = DeepSeekAnthropicMessagesConfig(tool_thinking="disabled").transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the weather tool."}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["tool_choice"] == {"type": "tool", "name": "get_weather"}
    assert request["thinking"] == {"type": "disabled"}


def test_deepseek_anthropic_messages_strips_adaptive_reasoning_controls():
    optional_params = {
        "max_tokens": 100,
        "thinking": {"type": "adaptive", "budget_tokens": 4096},
        "reasoning_effort": "high",
        "output_config": {"effort": "high", "format": {"type": "json_schema"}},
    }

    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params=optional_params,
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "enabled"}
    assert request["output_config"] == {"effort": "high"}
    assert "reasoning_effort" not in request
    assert optional_params["thinking"] == {"type": "adaptive", "budget_tokens": 4096}


@pytest.mark.parametrize(
    ("reasoning_effort", "expected_thinking", "expected_output_config"),
    [
        ("none", {"type": "disabled"}, None),
        ("low", {"type": "enabled"}, {"effort": "low"}),
        ("high", {"type": "enabled"}, {"effort": "high"}),
        ("max", {"type": "enabled"}, {"effort": "max"}),
    ],
)
def test_deepseek_anthropic_messages_maps_reasoning_effort_to_native_thinking(
    reasoning_effort, expected_thinking, expected_output_config
):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "reasoning_effort": reasoning_effort,
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == expected_thinking
    if expected_output_config is None:
        assert "output_config" not in request
    else:
        assert request["output_config"] == expected_output_config
    assert "reasoning_effort" not in request


def test_deepseek_anthropic_messages_disabled_thinking_drops_conflicting_effort():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "disabled"},
            "reasoning_effort": "max",
            "output_config": {"effort": "high"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in request
    assert "output_config" not in request


def test_deepseek_anthropic_messages_drops_unsupported_output_config():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "output_config": {"format": {"type": "json_schema"}},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert "output_config" not in request


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_deepseek_anthropic_messages_preserves_supported_output_effort(effort):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "output_config": {"effort": effort, "format": {"type": "json_schema"}},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["output_config"] == {"effort": effort}


@pytest.mark.parametrize(
    ("effort", "normalized_effort"),
    [("minimal", "low"), ("medium", "high"), ("xhigh", "high")],
)
def test_deepseek_anthropic_messages_normalizes_output_effort(effort, normalized_effort):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "output_config": {"effort": effort, "format": {"type": "json_schema"}},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["output_config"] == {"effort": normalized_effort}


@pytest.mark.parametrize("effort", ["none", "default", None, 1, {}, []])
def test_deepseek_anthropic_messages_drops_unsupported_output_effort(effort):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Hello"}],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "output_config": {"effort": effort, "format": {"type": "json_schema"}},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert "output_config" not in request


@pytest.mark.parametrize(
    "block_type",
    [
        "image",
        "document",
        "search_result",
        "code_execution_tool_result",
        "mcp_tool_use",
        "mcp_tool_result",
        "container_upload",
    ],
)
def test_deepseek_anthropic_messages_rejects_unsupported_content_blocks(block_type):
    with pytest.raises(litellm.utils.UnsupportedParamsError, match=block_type) as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": [{"type": block_type}]}],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize(
    "block_type",
    [
        "image",
        "document",
        "search_result",
        "code_execution_tool_result",
        "mcp_tool_use",
        "mcp_tool_result",
        "container_upload",
    ],
)
def test_deepseek_chat_bridge_rejects_unsupported_content_blocks(block_type):
    with pytest.raises(litellm.utils.UnsupportedParamsError, match=block_type) as error:
        prepare_deepseek_chat_history([{"role": "user", "content": [{"type": block_type}]}])

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize("surface", ["messages", "chat_bridge"])
def test_deepseek_rejects_unsupported_nested_tool_result_content(surface):
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [{"type": "image"}],
                }
            ],
        }
    ]

    with pytest.raises(litellm.utils.UnsupportedParamsError, match="image"):
        if surface == "messages":
            DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
                model="deepseek-v4-pro",
                messages=messages,
                anthropic_messages_optional_request_params={"max_tokens": 100},
                litellm_params=GenericLiteLLMParams(),
                headers={},
            )
        else:
            prepare_deepseek_chat_history(messages)


def test_deepseek_content_validation_has_bounded_nesting():
    nested_block = reduce(
        lambda content, _: {"type": "text", "content": [content]},
        range(2000),
        {"type": "text", "text": "Visible answer"},
    )

    _validate_deepseek_content_blocks([{"role": "user", "content": [nested_block]}])


@pytest.mark.parametrize("role", ["user", "tool"])
@pytest.mark.parametrize("surface", ["messages", "chat_bridge"])
def test_deepseek_rejects_redacted_thinking_outside_assistant_history(role, surface):
    messages = [{"role": role, "content": [{"type": "redacted_thinking", "data": "encrypted"}]}]

    with pytest.raises(litellm.utils.UnsupportedParamsError, match="redacted_thinking"):
        if surface == "messages":
            DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
                model="deepseek-v4-pro",
                messages=messages,
                anthropic_messages_optional_request_params={"max_tokens": 100},
                litellm_params=GenericLiteLLMParams(),
                headers={},
            )
        else:
            prepare_deepseek_chat_history(messages)


@pytest.mark.parametrize("surface", ["messages", "chat_bridge"])
def test_deepseek_rejects_nested_redacted_thinking(surface):
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ],
        }
    ]

    with pytest.raises(litellm.utils.UnsupportedParamsError, match="redacted_thinking"):
        if surface == "messages":
            DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
                model="deepseek-v4-pro",
                messages=messages,
                anthropic_messages_optional_request_params={"max_tokens": 100},
                litellm_params=GenericLiteLLMParams(),
                headers={},
            )
        else:
            prepare_deepseek_chat_history(messages)


def test_deepseek_content_validation_does_not_scan_tool_input_payloads():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Use the tool."},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "create_asset",
                        "input": {"type": "image"},
                    },
                ],
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"][1]["input"] == {"type": "image"}


@pytest.mark.parametrize(
    ("tool_use", "tool_arguments", "expected_tool_use"),
    [
        (
            {"type": "tool_use", "id": "call_123", "input": {"city": "Paris"}},
            "not-json",
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"city": "Paris"}},
        ),
        (
            {"type": "tool_use", "id": "call_123", "name": "get_weather"},
            '{"city":"Paris"}',
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"city": "Paris"}},
        ),
        (
            {"type": "tool_use", "id": "call_123"},
            '{"city":"Paris"}',
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"city": "Paris"}},
        ),
    ],
)
def test_deepseek_anthropic_messages_repairs_incomplete_tool_use_from_matching_tool_call(
    tool_use, tool_arguments, expected_tool_use
):
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [tool_use],
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": tool_arguments},
                    }
                ],
            }
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "disabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [expected_tool_use]


@pytest.mark.parametrize("missing_field", ["id", "name", "input"])
def test_deepseek_anthropic_messages_rejects_incomplete_tool_use_without_exact_source(missing_field):
    tool_use = {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}}
    tool_use.pop(missing_field)

    with pytest.raises(AnthropicError, match=rf"missing or has invalid {missing_field}") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [tool_use],
                    "tool_calls": [
                        {
                            "id": "call_other",
                            "type": "function",
                            "function": {"name": "other_tool", "arguments": "{}"},
                        }
                    ],
                }
            ],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert error.value.status_code == 400


@pytest.mark.parametrize(
    ("tool_use", "invalid_field"),
    [
        ({"type": "tool_use", "id": " ", "name": "get_weather", "input": {}}, "id"),
        ({"type": "tool_use", "id": "call_123", "name": " ", "input": {}}, "name"),
        ({"type": "tool_use", "id": "call_123", "name": "get_weather", "input": []}, "input"),
    ],
)
@pytest.mark.parametrize("has_matching_tool_call", [False, True])
def test_deepseek_anthropic_messages_rejects_invalid_tool_use_fields(tool_use, invalid_field, has_matching_tool_call):
    tool_calls = (
        [
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            }
        ]
        if has_matching_tool_call
        else []
    )
    with pytest.raises(AnthropicError, match=rf"missing or has invalid {invalid_field}") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[{"role": "assistant", "content": [tool_use], "tool_calls": tool_calls}],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert error.value.status_code == 400


def test_deepseek_anthropic_messages_rejects_ambiguous_tool_use_repair():
    duplicate_tool_call = {
        "id": "call_123",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{}"},
    }

    with pytest.raises(AnthropicError, match="missing or has invalid name") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_123", "input": {}}],
                    "tool_calls": [duplicate_tool_call, duplicate_tool_call],
                }
            ],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert error.value.status_code == 400


def test_deepseek_anthropic_messages_rejects_repair_from_non_function_tool_call():
    with pytest.raises(AnthropicError, match="missing or has invalid name") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_123", "input": {}}],
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "custom",
                            "function": {"name": "get_weather", "arguments": "{}"},
                        }
                    ],
                }
            ],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert error.value.status_code == 400


def test_deepseek_anthropic_messages_rejects_repair_from_malformed_arguments():
    with pytest.raises(AnthropicError, match="tool_calls contain invalid arguments") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_123", "name": "get_weather"}],
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": "not-json"},
                        }
                    ],
                }
            ],
            anthropic_messages_optional_request_params={
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert error.value.status_code == 400


def test_deepseek_anthropic_messages_preserves_complete_tool_use_over_matching_tool_call():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_123",
                        "name": "content_tool",
                        "input": {"source": "content"},
                    }
                ],
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "top_level_tool", "arguments": '{"source":"tool_calls"}'},
                    }
                ],
            }
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "disabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {
            "type": "tool_use",
            "id": "call_123",
            "name": "content_tool",
            "input": {"source": "content"},
        }
    ]


def test_deepseek_anthropic_messages_drops_non_tool_redacted_thinking():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "Visible answer"},
            ],
        }
    ]

    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [{"type": "text", "text": "Visible answer"}]
    assert messages[0]["content"][0]["type"] == "redacted_thinking"


def test_deepseek_anthropic_messages_rejects_redacted_only_history():
    with pytest.raises(AnthropicError, match="cannot replay redacted-only thinking") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize("content", [None, ""])
def test_deepseek_anthropic_messages_rejects_scalar_redacted_only_history(content):
    with pytest.raises(AnthropicError, match="cannot replay redacted-only thinking") as error:
        DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": content,
                    "thinking_blocks": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ],
            anthropic_messages_optional_request_params={"max_tokens": 100},
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize("content", [None, ""])
def test_deepseek_chat_bridge_rejects_scalar_redacted_only_history(content):
    with pytest.raises(AnthropicError, match="cannot replay redacted-only thinking") as error:
        prepare_deepseek_chat_history(
            [
                {
                    "role": "assistant",
                    "content": content,
                    "thinking_blocks": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ]
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


def test_deepseek_chat_bridge_allows_redacted_sidecar_with_tool_history_when_disabled():
    prepared = prepare_deepseek_chat_history(
        [
            {
                "role": "assistant",
                "content": None,
                "thinking_blocks": [{"type": "redacted_thinking", "data": "encrypted"}],
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            }
        ],
        require_reasoning=False,
    )

    assert prepared[0]["content"] is None
    assert prepared[0]["tool_calls"]
    assert "thinking_blocks" not in prepared[0]


def test_deepseek_anthropic_messages_drops_redacted_when_canonical_reasoning_exists():
    request = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
                "reasoning_content": "Use the weather tool.",
            }
        ],
        anthropic_messages_optional_request_params={"max_tokens": 100},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Use the weather tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]


def test_deepseek_chat_bridge_drops_redacted_when_canonical_reasoning_exists():
    prepared = prepare_deepseek_chat_history(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
                "thinking_blocks": [{"type": "thinking", "thinking": "stale sidecar", "signature": "old"}],
                "reasoning_content": "Use the weather tool.",
            }
        ]
    )

    assert prepared[0]["content"] == [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}]
    assert prepared[0]["thinking_blocks"] == [{"type": "thinking", "thinking": "Use the weather tool."}]


def test_deepseek_anthropic_messages_disables_thinking_without_promoting_tool_text():
    request = DeepSeekAnthropicMessagesConfig(tool_thinking="disabled").transform_anthropic_messages_request(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Use the weather tool.", "signature": "old"},
                    {"type": "text", "text": "I will use the tool."},
                    {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                ],
                "reasoning_content": "Canonical reasoning.",
            }
        ],
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled"},
        },
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == [
        {"type": "text", "text": "I will use the tool."},
        {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_redacted_tool_history_falls_back_before_deepseek_http():
    requests = []

    def mock_transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_fallback",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Fallback succeeded"}],
                "model": "claude-order-2",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    router = litellm.Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                    "order": 1,
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-order-2",
                    "api_base": "https://order-2.example.test",
                    "api_key": "test",
                    "order": 2,
                },
                "model_info": {"id": "order-2"},
            },
            {
                "model_name": "fallback",
                "litellm_params": {
                    "model": "anthropic/claude-fallback",
                    "api_base": "https://fallback.example.test",
                    "api_key": "test",
                },
                "model_info": {"id": "claude-fallback"},
            },
        ],
        fallbacks=[{"primary": ["fallback"]}],
        num_retries=1,
    )

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            response = await router.aanthropic_messages(
                max_tokens=100,
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "redacted_thinking", "data": "encrypted"},
                            {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                        ],
                    }
                ],
                model="primary",
                thinking={"type": "enabled"},
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert response["content"] == [{"type": "text", "text": "Fallback succeeded"}]
    assert len(requests) == 1
    assert requests[0].url.host == "order-2.example.test"
    assert router.total_calls["anthropic/claude-test"] == 1
    assert router.total_calls["anthropic/claude-order-2"] == 1
    assert router.total_calls["anthropic/claude-fallback"] == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_messages_error_does_not_retry_blocked_non_protocol_orders():
    requests = []

    def mock_transport(request):
        requests.append(request)
        return httpx.Response(
            400,
            request=request,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "upstream error"}},
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                    "order": 1,
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-order-2",
                    "api_base": "https://order-2.example.test",
                    "api_key": "test",
                    "order": 2,
                },
                "model_info": {"id": "order-2", "blocked": True},
            },
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-order-3",
                    "api_base": "https://order-3.example.test",
                    "api_key": "test",
                    "order": 3,
                },
                "model_info": {"id": "order-3", "blocked": True},
            },
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            with pytest.raises(BaseLLMException, match="upstream error"):
                await router.aanthropic_messages(
                    max_tokens=100,
                    messages=[{"role": "user", "content": "Hello"}],
                    model="primary",
                )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(requests) == 1
    assert router.total_calls["anthropic/claude-test"] == 1
    assert router.total_calls["anthropic/claude-order-2"] == 0
    assert router.total_calls["anthropic/claude-order-3"] == 0


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("messages_path", "expected_url"),
    [
        (None, "https://deepseek.example.test/anthropic/v1/messages"),
        ("anthropic/v1/messages", "https://deepseek.example.test/anthropic/v1/messages"),
        ("v1/messages", "https://deepseek.example.test/v1/messages"),
    ],
)
async def test_router_selected_deepseek_messages_replays_unsigned_thinking_to_mock_transport(
    messages_path, expected_url
):
    captured_requests = []

    def mock_transport(request):
        captured_requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/deepseek-v4-pro",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                },
                "model_info": {
                    "id": "deepseek-anthropic",
                    "reasoning_protocol": "deepseek_anthropic",
                    **({"deepseek_anthropic_messages_path": messages_path} if messages_path is not None else {}),
                },
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            response = await router.aanthropic_messages(
                max_tokens=100,
                messages=[
                    {"role": "user", "content": "Use the weather tool."},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "I should call the weather tool.", "signature": "claude"},
                            {
                                "type": "tool_use",
                                "id": "functions.Bash:0",
                                "name": "get_weather",
                                "input": {"city": "Paris"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "functions.Bash:0", "content": "Sunny"}],
                    },
                ],
                model="deepseek-group",
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert response["content"] == [{"type": "text", "text": "Done"}]
    assert captured_requests == [
        (
            expected_url,
            {
                "messages": [
                    {"role": "user", "content": "Use the weather tool."},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "I should call the weather tool."},
                            {
                                "type": "tool_use",
                                "id": "functions.Bash:0",
                                "name": "get_weather",
                                "input": {"city": "Paris"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "functions.Bash:0", "content": "Sunny"}],
                    },
                ],
                "max_tokens": 100,
                "model": "deepseek-v4-pro",
                "thinking": {"type": "enabled"},
            },
        )
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_router_configured_deepseek_messages_disables_tool_thinking_on_first_turn():
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
                "model": "deepseek-v4-flash",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                },
                "model_info": {
                    "id": "deepseek-anthropic",
                    "reasoning_protocol": "deepseek_anthropic",
                    "deepseek_anthropic_tool_thinking": "disabled",
                },
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    logging_obj = Logging(
        model="deepseek-group",
        messages=[{"role": "user", "content": "Use the weather tool."}],
        stream=False,
        call_type="anthropic_messages",
        start_time=time.time(),
        litellm_call_id="test-call-id",
        function_id="test-function-id",
    )

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            await router.aanthropic_messages(
                max_tokens=100,
                messages=[{"role": "user", "content": "Use the weather tool."}],
                model="deepseek-group",
                litellm_logging_obj=logging_obj,
                thinking={"type": "enabled"},
                tools=[
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "input_schema": {"type": "object"},
                    }
                ],
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"] == {"type": "disabled"}
    assert logging_obj.litellm_params["model_info"]["id"] == "deepseek-anthropic"


@pytest.mark.asyncio(loop_scope="module")
async def test_router_clears_deepseek_protocol_context_for_next_deployment():
    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {"model": "anthropic/claude-test", "api_key": "test"},
                "model_info": {
                    "id": "deepseek-anthropic",
                    "reasoning_protocol": "deepseek_anthropic",
                    "deepseek_anthropic_messages_path": "v1/messages",
                },
            },
            {
                "model_name": "claude-group",
                "litellm_params": {"model": "anthropic/claude-fallback", "api_key": "test"},
                "model_info": {"id": "claude"},
            },
        ],
        num_retries=0,
    )
    request_kwargs = {"litellm_metadata": {"model_group": "deepseek-group"}}

    try:
        deepseek_deployment = await router.async_get_available_deployment(
            model="deepseek-group", request_kwargs=request_kwargs, messages=[]
        )
        router._update_kwargs_with_deployment(
            deployment=deepseek_deployment,
            kwargs=request_kwargs,
            function_name="_ageneric_api_call_with_fallbacks",
        )
        assert "_litellm_deployment_protocol_context" in request_kwargs

        claude_deployment = await router.async_get_available_deployment(
            model="claude-group", request_kwargs=request_kwargs, messages=[]
        )
        router._update_kwargs_with_deployment(
            deployment=claude_deployment,
            kwargs=request_kwargs,
            function_name="_ageneric_api_call_with_fallbacks",
        )
    finally:
        router.discard()

    assert "_litellm_deployment_protocol_context" not in request_kwargs


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("reasoning_protocol", "expected_tool_type"),
    [("deepseek_anthropic", None), (None, "custom")],
)
async def test_router_selected_deepseek_chat_strips_custom_tool_type(reasoning_protocol, expected_tool_type):
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            content=(
                b'event: message_start\ndata: {"type":"message_start"}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            ),
        )

    model_info = {"id": "deployment"}
    if reasoning_protocol is not None:
        model_info["reasoning_protocol"] = reasoning_protocol
    router = litellm.Router(
        model_list=[
            {
                "model_name": "model-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://provider.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": model_info,
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        await router.acompletion(
            model="model-group",
            messages=[{"role": "user", "content": "Use the weather tool."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather.",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
            max_tokens=100,
            stream=True,
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    tool = captured_requests[0]["tools"][0]
    assert tool.get("type") == expected_tool_type
    assert tool["name"] == "get_weather"
    assert tool["input_schema"]["required"] == ["city"]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("stream", "reasoning_message_field"),
    [(False, "reasoning_content"), (True, "provider_specific_fields")],
)
async def test_router_selected_deepseek_chat_strips_reasoning_content_when_disabled(stream, reasoning_message_field):
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        if stream:
            return httpx.Response(
                200,
                request=request,
                content=(
                    b'event: message_start\ndata: {"type":"message_start"}\n\n'
                    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                ),
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    assistant_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
            }
        ],
    }
    if reasoning_message_field == "reasoning_content":
        assistant_message[reasoning_message_field] = "I should call the weather tool."
    else:
        assistant_message[reasoning_message_field] = {
            "reasoning_content": "I should call the weather tool.",
            "source": "provider",
        }

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        await router.acompletion(
            model="deepseek-group",
            messages=[
                {"role": "user", "content": "Use the weather tool."},
                assistant_message,
                {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather.",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            thinking={"type": "disabled"},
            allowed_openai_params=["thinking"],
            max_tokens=100,
            stream=stream,
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assistant_wire_message = captured_requests[0]["messages"][1]
    assert assistant_wire_message == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"city": "Paris"}},
        ],
    }
    assert captured_requests[0]["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_123", "content": "Sunny"}],
    }
    assert captured_requests[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    "assistant_message",
    [
        {
            "role": "assistant",
            "content": [{"type": "redacted_thinking", "data": "encrypted"}],
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "server_tool_use", "id": "srvtoolu_123", "name": "web_search", "input": {}},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "redacted_thinking", "data": "encrypted"}],
            "function_call": {"name": "get_weather", "arguments": "{}"},
        },
    ],
)
async def test_redacted_deepseek_chat_tool_history_falls_back_before_deepseek_http(assistant_message):
    requests = []

    def mock_transport(request):
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_fallback",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Fallback succeeded"}],
                "model": "claude-order-2",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def close_logging_coroutine(async_coroutine):
        async_coroutine.close()

    router = litellm.Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                    "order": 1,
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/claude-order-2",
                    "api_base": "https://order-2.example.test/v1/messages",
                    "api_key": "test",
                    "order": 2,
                },
                "model_info": {"id": "order-2"},
            },
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        with patch.object(
            GLOBAL_LOGGING_WORKER,
            "ensure_initialized_and_enqueue",
            side_effect=close_logging_coroutine,
        ):
            response = await router.acompletion(
                model="primary",
                messages=[
                    {"role": "user", "content": "Use the tool."},
                    assistant_message,
                ],
                max_tokens=100,
                thinking={"type": "enabled"},
                allowed_openai_params=["thinking"],
                client=http_client,
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert response.choices[0].message.content == "Fallback succeeded"
    assert len(requests) == 1
    assert requests[0].url.host == "order-2.example.test"
    assert router.total_calls["anthropic/claude-test"] == 1
    assert router.total_calls["anthropic/claude-order-2"] == 1


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("modify_params", [False, True])
async def test_deepseek_chat_tool_history_without_reasoning_reaches_http(modify_params):
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-flash",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        with patch.object(litellm, "modify_params", modify_params):
            response: Final = await router.acompletion(
                model="deepseek-group",
                messages=[
                    {"role": "user", "content": "Use the weather tool."},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
                ],
                max_tokens=100,
                thinking={"type": "enabled"},
                allowed_openai_params=["thinking"],
                client=http_client,
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert response.choices[0].message.content == "Done"
    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"]["type"] == "enabled"
    content: Final = captured_requests[0]["messages"][1]["content"]
    _assert_single_space_thinking_prefix(content)
    assert len(content) == 2
    assert content[1]["type"] == "tool_use"
    assert content[1]["id"] == "call_123"
    assert content[1]["input"]["city"] == "Paris"


@pytest.mark.asyncio(loop_scope="module")
async def test_router_configured_deepseek_chat_disables_tool_thinking_on_first_turn():
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
                "model": "deepseek-v4-flash",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-sonnet-4-6",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": {
                    "id": "deepseek-anthropic",
                    "reasoning_protocol": "deepseek_anthropic",
                    "deepseek_anthropic_tool_thinking": "disabled",
                },
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        await router.acompletion(
            model="deepseek-group",
            messages=[{"role": "user", "content": "Use the weather tool."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            thinking={"type": "enabled"},
            max_tokens=100,
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    "assistant_message",
    [
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_123",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_123",
                    "name": "web_search",
                    "input": {"query": "weather Paris"},
                }
            ],
        },
    ],
)
async def test_deepseek_chat_tool_history_shapes_without_reasoning_reach_http(assistant_message):
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        messages = [{"role": "user", "content": "Use the tool."}, assistant_message]
        if assistant_message.get("function_call") is not None:
            messages.append({"role": "function", "name": "get_weather", "content": "Sunny"})
        response: Final = await router.acompletion(
            model="deepseek-group",
            messages=messages,
            max_tokens=100,
            thinking={"type": "enabled"},
            allowed_openai_params=["thinking"],
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert response.choices[0].message.content == "Done"
    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"]["type"] == "enabled"
    assistant_content: Final = captured_requests[0]["messages"][1]["content"]
    _assert_single_space_thinking_prefix(assistant_content)
    assert any(block.get("type") in ("tool_use", "server_tool_use") for block in assistant_content[1:])


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_chat_deduplicates_native_and_openai_tool_history():
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Done"}],
                "model": "deepseek-v4-pro",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/deepseek-v4-pro",
                    "api_base": "https://deepseek.example.test/v1/messages",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        await router.acompletion(
            model="deepseek-group",
            messages=[
                {"role": "user", "content": "Use the tool."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I should check the weather."},
                        {"type": "text", "text": "Checking now."},
                        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
                        {"type": "text", "text": "The tool will provide the result."},
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_123",
                            "name": "web_search",
                            "input": {"query": "weather Paris"},
                        },
                    ],
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
            ],
            max_tokens=100,
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assistant_content = captured_requests[0]["messages"][1]["content"]
    assert assistant_content == [
        {"type": "thinking", "thinking": "I should check the weather."},
        {"type": "text", "text": "Checking now."},
        {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {}},
        {"type": "text", "text": "The tool will provide the result."},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_123",
            "name": "web_search",
            "input": {"query": "weather Paris"},
        },
    ]
    assert captured_requests[0]["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_123", "content": "Sunny"}],
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_anthropic_messages_streams_through_mock_transport():
    captured_requests = []

    def mock_transport(request):
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            content=(
                b'event: message_start\ndata: {"type":"message_start","reasoning_content":"foreign",'
                b'"provider_specific_fields":{"source":"foreign"}}\n\n'
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            ),
        )

    def close_logging_coroutine(async_coroutine):
        async_coroutine.close()

    router = litellm.Router(
        model_list=[
            {
                "model_name": "deepseek-group",
                "litellm_params": {
                    "model": "anthropic/deepseek-v4-pro",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek-anthropic", "reasoning_protocol": "deepseek_anthropic"},
            }
        ],
        num_retries=0,
    )
    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))

    try:
        with (
            patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client),
            patch.object(
                streaming_handler.GLOBAL_LOGGING_WORKER,
                "ensure_initialized_and_enqueue",
                side_effect=close_logging_coroutine,
            ),
        ):
            response = await router.aanthropic_messages(
                max_tokens=100,
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Call the tool.", "signature": "claude"},
                            {"type": "tool_use", "id": "functions.Bash:0", "name": "get_weather", "input": {}},
                        ],
                    }
                ],
                model="deepseek-group",
                stream=True,
            )
            chunks = [chunk async for chunk in response]
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert b"".join(chunks) == (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    assert captured_requests[0]["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "Call the tool."},
        {"type": "tool_use", "id": "functions.Bash:0", "name": "get_weather", "input": {}},
    ]
    assert captured_requests[0]["stream"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking_disabled", [False, True])
async def test_deepseek_anthropic_messages_native_stream_sanitizes_reasoning_fields(thinking_disabled):
    async def source():
        event = (
            b"event: content_block_start\ndata: "
            b'{"type":"content_block_start","index":0,"content_block":'
            b'{"type":"thinking","thinking":"","signature":"foreign"},'
            b'"reasoning_content":"foreign","provider_specific_fields":{"source":"foreign"}}\n\n'
            b"event: content_block_delta\ndata: "
            b'{"type":"content_block_delta","index":0,"delta":'
            b'{"type":"thinking_delta","thinking":"Think","signature":"foreign"}}\n\n'
            b"event: content_block_delta\ndata: "
            b'{"type":"content_block_delta","index":0,"delta":'
            b'{"type":"signature_delta","signature":"signed","reasoning_content":"foreign"}}\n\n'
            b"event: content_block_stop\ndata: "
            b'{"type":"content_block_stop","index":0}\n\n'
            b"event: content_block_start\ndata: "
            b'{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
            b"event: content_block_delta\ndata: "
            b'{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Done"}}\n\n'
            b"event: content_block_stop\ndata: "
            b'{"type":"content_block_stop","index":1}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        yield event[:37]
        yield event[37:]

    chunks = [
        chunk
        async for chunk in _sanitize_deepseek_messages_stream(
            source(),
            thinking_disabled=thinking_disabled,
        )
    ]
    output = b"".join(chunks)
    payloads = [json.loads(line[6:]) for line in output.splitlines() if line.startswith(b"data: ")]

    assert b"reasoning_content" not in output
    assert b"provider_specific_fields" not in output
    if thinking_disabled:
        assert [(payload["type"], payload.get("index")) for payload in payloads] == [
            ("content_block_start", 0),
            ("content_block_stop", 0),
            ("content_block_start", 1),
            ("content_block_delta", 1),
            ("content_block_stop", 1),
            ("message_stop", None),
        ]
        assert b"signature" not in output
        assert b'"type":"text"' in output
        assert b'"thinking"' not in output
    else:
        assert [(payload["type"], payload.get("index")) for payload in payloads] == [
            ("content_block_start", 0),
            ("content_block_delta", 0),
            ("content_block_delta", 0),
            ("content_block_stop", 0),
            ("content_block_start", 1),
            ("content_block_delta", 1),
            ("content_block_stop", 1),
            ("message_stop", None),
        ]
        assert b'"signature":"foreign"' in output
        assert b'"signature":"signed"' in output
        assert b'"type":"thinking"' in output
        assert b'"thinking":""' in output
        assert b'"thinking":"Think"' in output


@pytest.mark.asyncio
async def test_deepseek_anthropic_messages_native_stream_preserves_tool_input_fields():
    async def source():
        yield (
            b"id: event-123\nretry: 1000\n: upstream trace\n"
            b"event: content_block_start\ndata: "
            b'{"type":"content_block_start","index":0,"content_block":'
            b'{"type":"tool_use","id":"call_123","name":"inspect","input":'
            b'{"reasoning_content":"user data","thinking":{"value":"user data"}},'
            b'"caller":{"type":"direct"},'
            b'"reasoning_content":"foreign","provider_specific_fields":{"source":"foreign"}}}\n\n'
        )

    output = b"".join(
        [
            chunk
            async for chunk in _sanitize_deepseek_messages_stream(
                source(),
                thinking_disabled=False,
            )
        ]
    )
    payload = json.loads(next(line[6:] for line in output.splitlines() if line.startswith(b"data: ")))

    assert b"id: event-123\nretry: 1000\n: upstream trace\n" in output
    assert payload["content_block"] == {
        "type": "tool_use",
        "id": "call_123",
        "name": "inspect",
        "input": {"reasoning_content": "user data", "thinking": {"value": "user data"}},
        "caller": {"type": "direct"},
    }


@pytest.mark.asyncio
async def test_deepseek_anthropic_messages_native_stream_closes_source_on_early_exit():
    closed = MagicMock()

    async def source():
        try:
            yield b'event: message_start\ndata: {"type":"message_start"}\n\n'
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        finally:
            closed()

    stream = _sanitize_deepseek_messages_stream(source(), thinking_disabled=False)
    assert await anext(stream) == b'event: message_start\ndata: {"type":"message_start"}\n\n'
    await stream.aclose()

    closed.assert_called_once_with()


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_signature_error_does_not_retry_while_claude_keeps_retrying():
    signature_error = httpx.Response(
        400,
        text="Invalid signature in thinking block",
        request=httpx.Request("POST", "https://example.test/v1/messages"),
    )
    success = httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test/v1/messages"),
        json={
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "model": "claude-test",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    handler = BaseLLMHTTPHandler()
    logging_obj = MagicMock()
    logging_obj.model_call_details = {}

    deepseek_client = AsyncMock(spec=AsyncHTTPHandler)
    deepseek_client.post = AsyncMock(return_value=signature_error)
    with pytest.raises(Exception) as error:
        await handler.async_anthropic_messages_handler(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "Hello"}],
            anthropic_messages_provider_config=DeepSeekAnthropicMessagesConfig(),
            anthropic_messages_optional_request_params={"max_tokens": 100},
            custom_llm_provider="deepseek",
            litellm_params=GenericLiteLLMParams(),
            logging_obj=logging_obj,
            client=deepseek_client,
            api_key="test",
            api_base="https://deepseek.example.test",
        )
    assert deepseek_client.post.await_count == 1
    assert getattr(error.value, "_litellm_disable_fallbacks") is True

    claude_client = AsyncMock(spec=AsyncHTTPHandler)
    claude_client.post = AsyncMock(side_effect=[signature_error, success])
    response = await handler.async_anthropic_messages_handler(
        model="claude-test",
        messages=[
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "Think", "signature": "invalid"}],
            }
        ],
        anthropic_messages_provider_config=AnthropicMessagesConfig(),
        anthropic_messages_optional_request_params={"max_tokens": 100},
        custom_llm_provider="anthropic",
        litellm_params=GenericLiteLLMParams(),
        logging_obj=logging_obj,
        client=claude_client,
        api_key="test",
        api_base="https://claude.example.test",
    )

    assert response["content"] == [{"type": "text", "text": "Done"}]
    assert claude_client.post.await_count == 2
    first_claude_request = json.loads(claude_client.post.await_args_list[0].kwargs["data"])
    second_claude_request = json.loads(claude_client.post.await_args_list[1].kwargs["data"])
    assert first_claude_request["messages"] == [
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "Think", "signature": "invalid"}],
        }
    ]
    assert second_claude_request["messages"] == []


@pytest.mark.asyncio(loop_scope="module")
async def test_router_does_not_fallback_after_deepseek_signature_error():
    request_count = {"deepseek": 0, "fallback": 0}

    def mock_transport(request):
        provider = "fallback" if request.url.host == "fallback.example.test" else "deepseek"
        request_count[provider] += 1
        return httpx.Response(400, request=request, text="Invalid signature in thinking block")

    http_client = AsyncHTTPHandler()
    await http_client.client.aclose()
    http_client.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    router = litellm.Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "anthropic/deepseek-v4-pro",
                    "api_base": "https://deepseek.example.test",
                    "api_key": "test",
                },
                "model_info": {"id": "deepseek", "reasoning_protocol": "deepseek_anthropic"},
            },
            {
                "model_name": "fallback",
                "litellm_params": {
                    "model": "anthropic/claude-test",
                    "api_base": "https://fallback.example.test",
                    "api_key": "test",
                },
                "model_info": {"id": "fallback"},
            },
        ],
        fallbacks=[{"primary": ["fallback"]}],
        num_retries=0,
    )

    try:
        with patch("litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client", return_value=http_client):
            with pytest.raises(Exception) as error:
                await router.aanthropic_messages(
                    model="primary",
                    max_tokens=100,
                    messages=[
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "Call the tool.", "signature": "claude"},
                                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
                            ],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": "toolu_123", "content": "Sunny"}],
                        },
                    ],
                )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert getattr(error.value, "_litellm_disable_fallbacks") is True
    assert request_count == {"deepseek": 1, "fallback": 0}
