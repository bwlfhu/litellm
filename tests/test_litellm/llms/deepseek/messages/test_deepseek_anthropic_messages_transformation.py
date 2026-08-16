import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.anthropic.experimental_pass_through.messages.handler import anthropic_messages_handler
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.llms.deepseek.messages.transformation import (
    DeepSeekAnthropicMessagesConfig,
)
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.proxy.pass_through_endpoints import streaming_handler
from litellm.router import Router
from litellm.router_protocol import _RouterDeploymentProtocolContext
from litellm.types.router import GenericLiteLLMParams
from litellm.utils import ProviderConfigManager


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


@pytest.mark.asyncio
async def test_anthropic_messages_route_prioritizes_deepseek_protocol_deployment():
    router = Router(
        model_list=[
            {
                "model_name": "deepseek-v4-pro",
                "litellm_params": {"model": "openai/deepseek-v4-pro", "api_key": "test-key"},
                "model_info": {"id": "openai-deployment"},
            },
            {
                "model_name": "deepseek-v4-pro",
                "litellm_params": {
                    "model": "anthropic/deepseek-v4-pro",
                    "api_key": "test-key",
                    "custom_llm_provider": "anthropic",
                },
                "model_info": {"id": "deepseek-deployment", "reasoning_protocol": "deepseek_anthropic"},
            },
        ]
    )
    dispatch = AsyncMock(return_value={"content": []})
    routed_messages = router.factory_function(dispatch, call_type="anthropic_messages")

    await routed_messages(
        model="deepseek-v4-pro",
        max_tokens=100,
        messages=[{"role": "user", "content": "Hello"}],
    )

    assert dispatch.await_count == 1
    assert dispatch.call_args.kwargs["custom_llm_provider"] == "anthropic"
    assert dispatch.call_args.kwargs["model_info"]["id"] == "deepseek-deployment"
    assert "_litellm_router_call_type" not in dispatch.call_args.kwargs


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
        )

    assert result == "dispatched"
    config = dispatch.call_args.kwargs["anthropic_messages_provider_config"]
    assert isinstance(config, AnthropicMessagesConfig)
    assert not isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert "_deepseek_anthropic_messages_path" not in dispatch.call_args.kwargs["litellm_params"]


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
    ("api_base", "optional_params", "expected_url"),
    [
        ("https://api.deepseek.com", {}, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic", {}, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/anthropic/anthropic", {}, "https://api.deepseek.com/anthropic/v1/messages"),
        ("https://api.deepseek.com/v1/messages", {}, "https://api.deepseek.com/v1/messages"),
        (
            "https://api.deepseek.com/anthropic",
            {"_deepseek_anthropic_messages_path": "anthropic/v1/messages"},
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com/anthropic/v1",
            {"_deepseek_anthropic_messages_path": "anthropic/v1/messages"},
            "https://api.deepseek.com/anthropic/v1/messages",
        ),
        (
            "https://api.deepseek.com",
            {"_deepseek_anthropic_messages_path": "v1/messages"},
            "https://api.deepseek.com/v1/messages",
        ),
        (
            "https://api.deepseek.com/v1",
            {"_deepseek_anthropic_messages_path": "v1/messages"},
            "https://api.deepseek.com/v1/messages",
        ),
    ],
)
def test_deepseek_anthropic_messages_url_matrix(api_base, optional_params, expected_url):
    assert (
        DeepSeekAnthropicMessagesConfig().get_complete_url(
            api_base=api_base,
            api_key=None,
            model="deepseek-v4-pro",
            optional_params=optional_params,
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


def test_deepseek_anthropic_messages_replays_tool_history_without_thinking():
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

    request = config.transform_anthropic_messages_request(
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

    assert request["messages"] == original_messages
    assert request["thinking"] == {"type": "enabled"}
    assert request["output_config"] == {"effort": "high"}
    assert messages == original_messages


def test_deepseek_anthropic_messages_restores_reasoning_content_without_mutating_history():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}}],
            "provider_specific_fields": {"reasoning_content": "Use the weather tool.", "source": "upstream"},
        }
    ]

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
                {"type": "thinking", "thinking": "Use the weather tool."},
                {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
            ],
        }
    ]
    assert messages[0]["provider_specific_fields"]["reasoning_content"] == "Use the weather tool."


def test_deepseek_anthropic_messages_promotes_nonempty_provider_reasoning_content():
    response = DeepSeekAnthropicMessagesConfig().transform_anthropic_messages_response(
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
        logging_obj=MagicMock(),
    )

    assert response["content"][0] == {"type": "thinking", "thinking": "Use the tool."}
    assert response["provider_specific_fields"] == {"source": "deepseek"}


@pytest.mark.asyncio(loop_scope="module")
async def test_deepseek_redacted_tool_history_skips_http_and_router_fallback():
    requests = []

    def mock_transport(request):
        requests.append(request)
        return httpx.Response(200, request=request, json={})

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
            with pytest.raises(AnthropicError, match="DeepSeek Anthropic tool history") as error:
                await router.aanthropic_messages(
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
                )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert getattr(error.value, "_litellm_disable_fallbacks") is True
    assert requests == []
    assert router.total_calls["anthropic/claude-test"] == 1
    assert router.total_calls["anthropic/claude-order-2"] == 0
    assert router.total_calls["anthropic/claude-fallback"] == 0


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
                    "model": "anthropic/claude-test",
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
                "model": "claude-test",
                "stream": False,
            },
        )
    ]


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
async def test_router_selected_deepseek_chat_replays_reasoning_content(stream, reasoning_message_field):
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
            {"type": "thinking", "thinking": "I should call the weather tool."},
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
async def test_redacted_deepseek_chat_tool_history_skips_http_and_fallback(assistant_message):
    requests = []

    def mock_transport(request):
        requests.append(request)
        return httpx.Response(200, request=request, json={})

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
        with pytest.raises(litellm.BadRequestError, match="DeepSeek Anthropic tool history") as error:
            await router.acompletion(
                model="primary",
                messages=[
                    {"role": "user", "content": "Use the tool."},
                    assistant_message,
                ],
                max_tokens=100,
                client=http_client,
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert getattr(error.value, "_litellm_disable_fallbacks") is True
    assert requests == []
    assert router.total_calls["anthropic/claude-test"] == 1
    assert router.total_calls["anthropic/claude-order-2"] == 0


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("modify_params", [False, True])
async def test_deepseek_chat_tool_history_without_reasoning_disables_thinking(modify_params):
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
            await router.acompletion(
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
                client=http_client,
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"] == {"type": "disabled"}
    assert captured_requests[0]["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"city": "Paris"}}],
    }


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("assistant_message", "expected_tool_type"),
    [
        (
            {
                "role": "assistant",
                "content": None,
                "function_call": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
            },
            "tool_use",
        ),
        (
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
            "tool_use",
        ),
        (
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
            "server_tool_use",
        ),
    ],
)
async def test_deepseek_chat_non_tool_calls_history_without_reasoning_disables_thinking(
    assistant_message, expected_tool_type
):
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
        await router.acompletion(
            model="deepseek-group",
            messages=messages,
            max_tokens=100,
            client=http_client,
        )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await http_client.client.aclose()
        router.discard()

    assert len(captured_requests) == 1
    assert captured_requests[0]["thinking"] == {"type": "disabled"}
    wire_tool_block = captured_requests[0]["messages"][1]["content"][0]
    assert wire_tool_block["type"] == expected_tool_type
    if isinstance(assistant_message["content"], list):
        assert wire_tool_block == assistant_message["content"][0]
    if assistant_message.get("function_call") is not None:
        assert wire_tool_block["id"] == "legacy_function_call_1"
        assert captured_requests[0]["messages"][2] == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "legacy_function_call_1", "content": "Sunny"}],
        }


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
                b'event: message_start\ndata: {"type":"message_start"}\n\n'
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
    with pytest.raises(Exception):
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
