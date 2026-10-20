import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Final

import httpx
import pytest

import litellm
from litellm.caching.llm_caching_handler import LLMClientCache
from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig
from litellm.router_protocol import build_deployment_protocol_context
from litellm.types.llms.openai import AllMessageValues


@pytest.fixture
def deepseek_vision_models():
    model_names = (
        "deepseek-v4-flash-vision-exp",
        "deepseek/deepseek-v4-flash-vision-exp",
        "deepseek-flash",
        "deepseek/deepseek-flash",
    )
    prior_entries = {name: litellm.model_cost.get(name) for name in model_names}
    litellm.register_model(
        {
            name: {
                "litellm_provider": "deepseek",
                "mode": "chat",
                "supports_vision": True,
            }
            for name in model_names
        }
    )
    yield
    for name, prior_entry in prior_entries.items():
        if prior_entry is None:
            litellm.model_cost.pop(name, None)
        else:
            litellm.model_cost[name] = prior_entry


@pytest.fixture
def deepseek_always_on_models():
    model_names = (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek/review-prefixed-only-v4",
    )
    prior_entries = {name: litellm.model_cost.get(name) for name in model_names}
    litellm.register_model(
        {
            name: {
                "litellm_provider": "deepseek",
                "mode": "chat",
                "supports_reasoning": True,
                "thinking_always_on": True,
            }
            for name in model_names
        }
    )
    yield
    for name, prior_entry in prior_entries.items():
        if prior_entry is None:
            litellm.model_cost.pop(name, None)
        else:
            litellm.model_cost[name] = prior_entry


DEEPSEEK_API_BASE: Final = "https://api.deepseek.com/beta"
DEEPSEEK_CHAT_COMPLETIONS_URL: Final = f"{DEEPSEEK_API_BASE}/chat/completions"
DEEPSEEK_API_KEY: Final = "fake_api_key"


@pytest.fixture
def _deepseek_httpx_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    client_cache: Final = LLMClientCache()
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", client_cache)
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", True)
    monkeypatch.setattr(litellm, "force_ipv4", False)
    monkeypatch.setattr(litellm, "sync_transport", None, raising=False)
    yield
    client_cache.flush_cache()


def _deepseek_response(stream: bool) -> httpx.Response:
    if stream:
        chunks: Final = (
            {
                "id": "chatcmpl-deepseek",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-reasoner",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Hello!"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-deepseek",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-reasoner",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        )
        body: Final = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-deepseek",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-reasoner",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _assert_deepseek_request(request: httpx.Request, messages: list[dict[str, str]], stream: bool) -> None:
    assert str(request.url) == DEEPSEEK_CHAT_COMPLETIONS_URL
    assert request.headers["Authorization"] == f"Bearer {DEEPSEEK_API_KEY}"
    actual_data: Final = json.loads(request.content)
    assert actual_data["model"] == "deepseek-reasoner"
    assert actual_data["messages"] == messages
    assert actual_data["stream"] is stream


def _function_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object"}},
    }


def _function_tool_call(name: str) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": '{"path":"/tmp/result"}'},
    }


@pytest.mark.parametrize(
    ("reasoning_effort", "expected_effort"),
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "max"),
    ],
)
def test_map_openai_params_preserves_supported_reasoning_effort(reasoning_effort, expected_effort):
    result = DeepSeekChatConfig().map_openai_params(
        non_default_params={"reasoning_effort": reasoning_effort},
        optional_params={},
        model="deepseek-v4-pro",
        drop_params=False,
    )

    assert result["thinking"] == {"type": "enabled"}
    assert result["reasoning_effort"] == expected_effort


def test_map_openai_params_none_effort_disables_thinking_without_sending_effort():
    result = DeepSeekChatConfig().map_openai_params(
        non_default_params={"reasoning_effort": "none"},
        optional_params={},
        model="deepseek-v4-pro",
        drop_params=False,
    )

    assert result["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in result


def test_map_openai_params_disabled_thinking_omits_conflicting_effort():
    result = DeepSeekChatConfig().map_openai_params(
        non_default_params={
            "thinking": {"type": "disabled"},
            "reasoning_effort": "max",
        },
        optional_params={},
        model="deepseek-v4-pro",
        drop_params=False,
    )

    assert result["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in result


def test_drop_unsupported_tools_keeps_function_tools_only():
    optional_params = {
        "tools": [
            _function_tool("shell"),
            {"type": "namespace", "name": "container.exec"},
            _function_tool("apply_patch"),
        ],
        "tool_choice": "auto",
    }

    result = DeepSeekChatConfig._drop_unsupported_tools(optional_params)

    assert [tool["function"]["name"] for tool in result["tools"]] == [
        "shell",
        "apply_patch",
    ]
    assert all(tool["type"] == "function" for tool in result["tools"])
    assert result["tool_choice"] == "auto"


def test_drop_unsupported_tools_drops_dangling_tool_choice_when_none_survive():
    optional_params = {
        "tools": [{"type": "namespace", "name": "container.exec"}],
        "tool_choice": "required",
        "parallel_tool_calls": True,
        "temperature": 0.2,
    }

    result = DeepSeekChatConfig._drop_unsupported_tools(optional_params)

    assert "tools" not in result
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result
    assert result["temperature"] == 0.2


def test_drop_unsupported_tools_is_noop_for_function_only():
    optional_params = {
        "tools": [_function_tool("shell")],
        "tool_choice": "auto",
    }

    result = DeepSeekChatConfig._drop_unsupported_tools(optional_params)

    assert result is optional_params


def test_drop_unsupported_tools_is_noop_without_tools():
    optional_params = {"temperature": 0.7}

    result = DeepSeekChatConfig._drop_unsupported_tools(optional_params)

    assert result is optional_params


def test_transform_request_strips_unsupported_tools_from_body():
    config = DeepSeekChatConfig()
    body = config.transform_request(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={
            "tools": [
                _function_tool("shell"),
                {"type": "namespace", "name": "container.exec"},
            ],
            "tool_choice": "auto",
        },
        litellm_params={},
        headers={},
    )

    assert [tool["type"] for tool in body["tools"]] == ["function"]
    assert body["tools"][0]["function"]["name"] == "shell"


async def test_async_transform_request_strips_unsupported_tools_from_body():
    config = DeepSeekChatConfig()
    body = await config.async_transform_request(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={
            "tools": [
                _function_tool("shell"),
                {"type": "namespace", "name": "container.exec"},
            ],
            "tool_choice": "auto",
        },
        litellm_params={},
        headers={},
    )

    assert [tool["type"] for tool in body["tools"]] == ["function"]
    assert body["tools"][0]["function"]["name"] == "shell"


def test_thinking_mode_active_bool_thinking_returns_false_without_crashing():
    config = DeepSeekChatConfig()
    assert config._thinking_mode_active(model="deepseek-reasoner", optional_params={"thinking": True}) is False


class TestDeepSeekVisionMultimodalContent:
    """Image content lists are forwarded only for user messages on vision models."""

    VISION_MODEL = "deepseek/deepseek-v4-flash-vision-exp"
    NON_VISION_MODEL = "deepseek/deepseek-chat"

    def setup_method(self):
        self.config = DeepSeekChatConfig()
        prior_entry = litellm.model_cost.get(self.VISION_MODEL)
        self._prior_registry_entry = dict(prior_entry) if prior_entry is not None else None
        litellm.register_model(
            {
                "deepseek/deepseek-v4-flash-vision-exp": {
                    "litellm_provider": "deepseek",
                    "mode": "chat",
                    "input_cost_per_token": 4.4e-07,
                    "output_cost_per_token": 1.32e-06,
                    "supports_vision": True,
                }
            }
        )

    def teardown_method(self):
        if self._prior_registry_entry is None:
            litellm.model_cost.pop(self.VISION_MODEL, None)
        else:
            litellm.model_cost[self.VISION_MODEL] = self._prior_registry_entry

    @staticmethod
    def _image_message(role="user"):
        return {
            "role": role,
            "content": [
                {"type": "text", "text": "what is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/image.jpg", "detail": "auto"},
                },
            ],
        }

    def test_user_image_list_forwarded_on_vision_model(self):
        result = self.config.transform_messages([self._image_message()], model=self.VISION_MODEL)

        assert isinstance(result[0]["content"], list)
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][1]["type"] == "image_url"
        assert result[0]["content"][1]["image_url"]["url"] == "https://example.com/image.jpg"

    def test_image_list_collapsed_on_non_vision_model(self):
        result = self.config.transform_messages([self._image_message()], model=self.NON_VISION_MODEL)

        assert result[0]["content"] == "what is in this image?"

    def test_image_list_collapsed_on_non_user_roles_even_on_vision_model(self):
        for role in ("assistant", "system"):
            result = self.config.transform_messages([self._image_message(role=role)], model=self.VISION_MODEL)

            assert result[0]["content"] == "what is in this image?"

    def test_audio_block_collapsed_even_on_vision_model(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "transcribe this"},
                    {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0]["content"] == "transcribe this"

    def test_typeless_image_block_collapses(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"image_url": {"url": "https://example.com/image.jpg"}},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0]["content"] == "what is this"

    def test_text_only_content_list_collapses(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert isinstance(result[0]["content"], str)
        assert result[0]["content"] == "Hello world"

    def test_search_results_text_appended_on_forwarded_message(self):
        message = self._image_message()
        message["search_results"] = [{"source": "kb", "content": [{"text": "article body"}]}]

        result = self.config.transform_messages([message], model=self.VISION_MODEL)

        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[-1] == {"type": "text", "text": "kbarticle body"}
        assert any(block.get("type") == "image_url" for block in content)
        assert "search_results" not in result[0]

    def test_search_results_text_kept_on_collapse(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "context: "}],
                "search_results": [{"source": "kb", "content": [{"text": "article body"}]}],
            }
        ]

        result = self.config.transform_messages(messages, model=self.NON_VISION_MODEL)

        assert result[0]["content"] == "context: kbarticle body"

    def test_responses_shape_blocks_collapse_even_on_vision_model(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0]["content"] == "what is this?"

    def test_image_block_missing_payload_collapses(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0]["content"] == "hi"

    def test_image_block_empty_payload_object_collapses(self):
        for payload in ({}, {"url": ""}, {"detail": "auto"}, None, 42):
            messages = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": payload}],
                }
            ]

            result = self.config.transform_messages(messages, model=self.VISION_MODEL)

            assert result[0]["content"] == "hi"

    def test_image_block_string_payload_forwarded(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": "https://example.com/image.jpg"},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        content = result[0]["content"]
        assert isinstance(content, list)
        assert content[1]["image_url"] == {"url": "https://example.com/image.jpg"}

    def test_text_block_missing_text_field_collapses(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "text"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
                ],
            }
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0]["content"] == "hi"

    def test_string_content_search_results_folded_into_string(self):
        messages = [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "summarize the docs",
                "search_results": [{"source": "kb", "content": [{"text": "article body"}]}],
            }
        ]

        result = self.config.transform_messages(messages, model=self.NON_VISION_MODEL)

        assert result[0]["content"] == "summarize the docskbarticle body"

    def test_plain_string_content_message_unchanged(self):
        messages = [{"role": "user", "content": "hello"}]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert result[0] is messages[0]

    def test_empty_content_list_untouched(self):
        messages = [{"role": "user", "content": []}]

        result = self.config.transform_messages(messages, model=self.NON_VISION_MODEL)

        assert result[0]["content"] == []

    def test_later_messages_still_collapsed_after_forwarded_one(self):
        messages = [
            self._image_message(),
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "and "},
                    {"type": "text", "text": "then?"},
                ],
            },
            self._image_message(),
        ]

        result = self.config.transform_messages(messages, model=self.VISION_MODEL)

        assert isinstance(result[0]["content"], list)
        assert result[1]["content"] == "and then?"
        assert isinstance(result[2]["content"], list)

    def test_transform_request_preserves_image_url_block(self):
        body = self.config.transform_request(
            model=self.VISION_MODEL,
            messages=[self._image_message()],
            optional_params={},
            litellm_params={},
            headers={},
        )

        content = body["messages"][0]["content"]
        assert isinstance(content, list)
        assert any(block.get("type") == "image_url" for block in content)

    async def test_async_transform_request_preserves_image_url_block(self):
        body = await self.config.async_transform_request(
            model=self.VISION_MODEL,
            messages=[self._image_message()],
            optional_params={},
            litellm_params={},
            headers={},
        )

        content = body["messages"][0]["content"]
        assert isinstance(content, list)
        assert any(block.get("type") == "image_url" for block in content)


class TestDeepSeekThinkingParams:
    """Test thinking and reasoning_effort parameter handling for DeepSeek."""

    def setup_method(self):
        self.config = DeepSeekChatConfig()
        self.model = "deepseek-reasoner"

    def test_get_supported_openai_params_includes_thinking(self):
        """Test that thinking and reasoning_effort are in supported params."""
        params = self.config.get_supported_openai_params(self.model)
        assert "thinking" in params
        assert "reasoning_effort" in params

    def test_map_thinking_enabled(self):
        """Test that thinking={"type": "enabled"} is passed through correctly."""
        non_default_params = {"thinking": {"type": "enabled"}}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert result["thinking"] == {"type": "enabled"}

    def test_map_thinking_with_budget_tokens_strips_budget(self):
        """Test that budget_tokens is stripped from thinking param (DeepSeek doesn't support it)."""
        non_default_params = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        # Should strip budget_tokens, only pass type
        assert result["thinking"] == {"type": "enabled"}
        assert "budget_tokens" not in result.get("thinking", {})

    def test_map_reasoning_effort_medium(self):
        """Test that reasoning_effort='medium' maps to thinking enabled."""
        non_default_params = {"reasoning_effort": "medium"}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert result["thinking"] == {"type": "enabled"}

    def test_map_reasoning_effort_low(self):
        """Test that reasoning_effort='low' maps to thinking enabled."""
        non_default_params = {"reasoning_effort": "low"}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert result["thinking"] == {"type": "enabled"}

    def test_map_reasoning_effort_high(self):
        """Test that reasoning_effort='high' maps to thinking enabled."""
        non_default_params = {"reasoning_effort": "high"}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert result["thinking"] == {"type": "enabled"}

    def test_map_reasoning_effort_none_does_not_enable_thinking(self):
        """Test that reasoning_effort='none' does not enable thinking."""
        non_default_params = {"reasoning_effort": "none"}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert result["thinking"] == {"type": "disabled"}

    def test_map_reasoning_effort_null_does_not_enable_thinking(self):
        """Test that reasoning_effort=None does not enable thinking."""
        non_default_params = {"reasoning_effort": None}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert "thinking" not in result

    def test_thinking_takes_precedence_over_reasoning_effort(self):
        """Test that thinking param takes precedence when both are provided."""
        non_default_params = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        # thinking should be set, reasoning_effort should not override
        assert result["thinking"] == {"type": "enabled"}

    def test_invalid_thinking_type_ignored(self):
        """Test that invalid thinking type values are ignored."""
        non_default_params = {"thinking": {"type": "invalid"}}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert "thinking" not in result

    def test_thinking_none_value_ignored(self):
        """Test that thinking=None is ignored."""
        non_default_params = {"thinking": None}
        optional_params = {}

        result = self.config.map_openai_params(
            non_default_params=non_default_params,
            optional_params=optional_params,
            model=self.model,
            drop_params=False,
        )

        assert "thinking" not in result

    def test_drop_unsupported_tools_removes_dangling_tool_choice(self):
        optional_params = {
            "tools": [
                {"type": "namespace", "name": "local_shell"},
                {"type": "function", "function": {"name": "get_weather"}},
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": "local_shell"},
            },
            "parallel_tool_calls": True,
        }

        result = self.config._drop_unsupported_tools(optional_params)

        assert result["tools"] == [{"type": "function", "function": {"name": "get_weather"}}]
        assert "tool_choice" not in result
        assert result["parallel_tool_calls"] is True


@pytest.mark.parametrize("stream", [True, False])
def test_deepseek_mock_completion(stream):
    """
    Deepseek API is hanging. Mock the call, to a fake endpoint, so we can confirm our integration is working.
    """
    import litellm
    from litellm import completion

    litellm.turn_on_debug()

    response = completion(
        model="deepseek/deepseek-reasoner",
        messages=[{"role": "user", "content": "Hello, world!"}],
        api_base="https://exampleopenaiendpoint-production.up.railway.app/v1/chat/completions",
        stream=stream,
        mock_response="Hello! How can I help you today?",
    )
    print(f"response: {response}")
    if stream:
        for chunk in response:
            print(chunk)
    else:
        assert response is not None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_deepseek_provider_async_completion(stream):
    """
    Test that Deepseek provider requests are formatted correctly with the proper parameters
    """
    import json
    from unittest.mock import MagicMock, patch

    import litellm
    from litellm import acompletion

    litellm.turn_on_debug()

    # Set up the test parameters
    api_key = "fake_api_key"
    model = "deepseek/deepseek-reasoner"
    messages = [{"role": "user", "content": "Hello, world!"}]

    # Mock AsyncHTTPHandler.post method for async test
    with patch("litellm.llms.custom_httpx.llm_http_handler.AsyncHTTPHandler.post") as mock_post:
        mock_response_data = litellm.ModelResponse(
            choices=[
                litellm.Choices(
                    message=litellm.Message(content="Hello!"),
                    index=0,
                    finish_reason="stop",
                )
            ]
        ).model_dump()
        # Create a proper mock response
        mock_response = MagicMock()  # Use MagicMock instead of AsyncMock
        mock_response.status_code = 200
        mock_response.text = json.dumps(mock_response_data)
        mock_response.headers = {"Content-Type": "application/json"}

        # Make json() return a value directly, not a coroutine
        mock_response.json.return_value = mock_response_data

        # Set the return value for the post method
        mock_post.return_value = mock_response

        await acompletion(
            custom_llm_provider="deepseek",
            api_key=api_key,
            model=model,
            messages=messages,
            stream=stream,
        )

    # Verify the request was made with the correct parameters
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    print("request call=", json.dumps(call_args.kwargs, indent=4, default=str))

    # Check request body
    request_body = json.loads(call_args.kwargs["data"])
    assert call_args.kwargs["url"] == "https://api.deepseek.com/beta/chat/completions"
    assert request_body["model"] == "deepseek-reasoner"  # Model name should be stripped of provider prefix
    assert request_body["messages"] == messages
    assert request_body["stream"] == stream


def test_deepseek_fill_reasoning_content_multiturn():
    """
    Unit test for _fill_reasoning_content.
    Reproduces issue #28045: DeepSeek thinking mode fails in multi-turn conversations
    because reasoning_content is not passed back to the API.
    """
    from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig

    config = DeepSeekChatConfig()

    # Case 1: assistant message already has reasoning_content — should be left as-is
    messages_with_rc = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi", "reasoning_content": "I thought about it"},
        {"role": "user", "content": "Follow up"},
    ]
    result = config._fill_reasoning_content(
        messages_with_rc, model="deepseek-reasoner", litellm_params={}, require_reasoning=True
    )
    assert result[1]["reasoning_content"] == "I thought about it"

    # Case 2: assistant message has reasoning_content in provider_specific_fields — should be promoted
    messages_with_psf = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "Hi",
            "provider_specific_fields": {"reasoning_content": "stored thinking"},
        },
        {"role": "user", "content": "Follow up"},
    ]
    result = config._fill_reasoning_content(
        messages_with_psf, model="deepseek-reasoner", litellm_params={}, require_reasoning=True
    )
    assert result[1]["reasoning_content"] == "stored thinking"
    # Should be removed from provider_specific_fields to avoid duplication
    assert "reasoning_content" not in result[1].get("provider_specific_fields", {})

    # Case 3: assistant message has no reasoning_content anywhere — should inject placeholder
    messages_no_rc = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "Hi",
            "tool_calls": [{"id": "c", "type": "function", "function": {"name": "tool", "arguments": "{}"}}],
        },
        {"role": "user", "content": "Follow up"},
    ]
    result = config._fill_reasoning_content(
        messages_no_rc, model="deepseek-reasoner", litellm_params={}, require_reasoning=True
    )
    assert result[1]["reasoning_content"] == " "

    # Case 4: non-assistant messages should never be touched
    messages_user_only = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": "You are helpful"},
    ]
    result = config._fill_reasoning_content(
        messages_user_only, model="deepseek-reasoner", litellm_params={}, require_reasoning=True
    )
    assert "reasoning_content" not in result[0]
    assert "reasoning_content" not in result[1]


def test_deepseek_fill_reasoning_content_guard_in_transform_request():
    """
    _fill_reasoning_content must only run when BOTH conditions are true:
      1. supports_reasoning() is True for the model
      2. thinking mode is explicitly enabled in optional_params ({"type": "enabled"})

    This prevents spurious injection on models like deepseek-v3.2 that support
    thinking as opt-in but not always-on. Addresses oss-pr-review-agent feedback
    on PR #28057.
    """
    from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig

    config = DeepSeekChatConfig()

    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "Hi",
            "tool_calls": [{"id": "c", "type": "function", "function": {"name": "tool", "arguments": "{}"}}],
        },
        {"role": "user", "content": "Follow up"},
    ]

    # Case 1: reasoning model + thinking enabled -> injection should happen
    result = config.transform_request(
        model="deepseek-reasoner",
        messages=messages,
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )
    assert result["messages"][1].get("reasoning_content") == " ", (
        "reasoning_content should be injected when thinking is enabled"
    )

    # Case 2: reasoning model + thinking NOT in optional_params -> no injection
    result = config.transform_request(
        model="deepseek-reasoner",
        messages=messages,
        optional_params={},
        litellm_params={},
        headers={},
    )
    assert "reasoning_content" not in result["messages"][1], (
        "reasoning_content should not be injected when thinking is not enabled"
    )

    # Case 3: non-reasoning model + thinking enabled -> no injection
    result = config.transform_request(
        model="deepseek-chat",
        messages=messages,
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )
    assert "reasoning_content" not in result["messages"][1], (
        "reasoning_content should not be injected for non-reasoning models"
    )


@pytest.mark.parametrize(
    "tool_choice",
    [
        "required",
        {"type": "required"},
        {"type": "function", "function": {"name": "shell"}},
    ],
)
def test_transform_request_normalizes_forced_tool_choice_when_thinking_is_enabled(
    tool_choice,
    deepseek_always_on_models,
):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the shell tool"}],
        optional_params={
            "thinking": {"type": "enabled"},
            "tools": [_function_tool("shell")],
            "tool_choice": tool_choice,
        },
        litellm_params={},
        headers={},
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["tool_choice"] == "auto"


def test_transform_request_normalizes_forced_tool_choice_when_v4_thinking_defaults_on(deepseek_always_on_models):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the shell tool"}],
        optional_params={
            "tools": [_function_tool("shell")],
            "tool_choice": "required",
        },
        litellm_params={},
        headers={},
    )

    assert body["tool_choice"] == "auto"


@pytest.mark.parametrize(
    ("thinking", "tool_choice"),
    [
        ({"type": "disabled"}, "required"),
        ({"type": "disabled"}, {"type": "function", "function": {"name": "shell"}}),
        ({"type": "enabled"}, "auto"),
        ({"type": "enabled"}, "none"),
    ],
)
def test_transform_request_preserves_supported_tool_choice_modes(
    thinking,
    tool_choice,
    deepseek_always_on_models,
):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the shell tool"}],
        optional_params={
            "thinking": thinking,
            "tools": [_function_tool("shell")],
            "tool_choice": tool_choice,
        },
        litellm_params={},
        headers={},
    )

    assert body["tool_choice"] == tool_choice


async def test_async_transform_request_normalizes_forced_tool_choice_when_thinking_is_enabled(
    deepseek_always_on_models,
):
    body = await DeepSeekChatConfig().async_transform_request(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "Use the shell tool"}],
        optional_params={
            "thinking": {"type": "enabled"},
            "tools": [_function_tool("shell")],
            "tool_choice": "required",
        },
        litellm_params={},
        headers={},
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["tool_choice"] == "auto"


def test_vision_model_preserves_user_image_content_list(deepseek_vision_models):
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/image.jpg", "detail": "auto"},
            },
        ],
    }

    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-flash-vision-exp",
        messages=[message],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["content"] == message["content"]


async def test_async_vision_model_preserves_image_and_search_results(deepseek_vision_models):
    body = await DeepSeekChatConfig().async_transform_request(
        model="deepseek/deepseek-v4-flash-vision-exp",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Use this context"},
                    {"type": "image_url", "image_url": "https://example.com/image.jpg"},
                ],
                "search_results": [{"source": "kb", "content": [{"text": "result"}]}],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["content"][-1] == {"type": "text", "text": "kbresult"}
    assert "search_results" not in body["messages"][0]


@pytest.mark.parametrize("search_results", [[], [{"rank": 1}]])
def test_vision_model_drops_search_results_without_extractable_text(deepseek_vision_models, search_results):
    content = [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
    ]

    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-flash-vision-exp",
        messages=[{"role": "user", "content": content, "search_results": search_results}],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["content"] == content
    assert "search_results" not in body["messages"][0]


@pytest.mark.parametrize("model", ["deepseek-chat"])
def test_non_vision_model_collapses_image_content_list(model):
    body = DeepSeekChatConfig().transform_request(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}},
                ],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["content"] == "Describe this image"


def test_vision_model_collapses_malformed_image_content_list(deepseek_vision_models):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-flash-vision-exp",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {}},
                ],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["content"] == "Describe this image"


def test_thinking_mode_does_not_fill_ordinary_assistant_reasoning():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "An ordinary answer"},
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert "reasoning_content" not in body["messages"][1]


def test_thinking_mode_backfills_missing_reasoning_for_tool_history():
    tool_call = _function_tool_call("shell")
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            },
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == " "
    assert body["messages"][1]["tool_calls"] == [tool_call]


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek/deepseek-v4-pro"])
def test_v4_thinking_mode_is_active_by_default(model, deepseek_always_on_models):
    body = DeepSeekChatConfig().transform_request(
        model=model,
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_function_tool("shell")],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["reasoning_content"] == " "


def test_reasoning_capable_v3_thinking_mode_requires_explicit_enablement():
    config = DeepSeekChatConfig()

    assert config._thinking_mode_active(model="deepseek-v3.2", optional_params={}) is False
    assert (
        config._thinking_mode_active(
            model="deepseek-v3.2",
            optional_params={"thinking": {"type": "enabled"}},
        )
        is True
    )


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek/deepseek-v4-flash"])
def test_registered_v4_chat_models_default_to_thinking(model, deepseek_always_on_models):
    config = DeepSeekChatConfig()

    assert config._thinking_mode_active(model=model, optional_params={}) is True


def test_prefixed_model_metadata_preserves_thinking_always_on(deepseek_always_on_models):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek/review-prefixed-only-v4",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_function_tool("shell")],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["reasoning_content"] == " "


@pytest.mark.parametrize("model", ["router-alias", "deepseek-v4-future"])
def test_unknown_models_do_not_activate_thinking(model):
    assert (
        DeepSeekChatConfig()._thinking_mode_active(
            model=model,
            optional_params={},
        )
        is False
    )


def test_chat_history_strips_signature_fields_from_messages():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": "Done",
                "signature": "foreign",
                "thought_signature": "foreign",
                "reasoning_content": "Solve the task.",
            }
        ],
        optional_params={"thinking": {"type": "disabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"] == [{"role": "assistant", "content": "Done"}]


def test_v4_thinking_mode_can_be_disabled_explicitly():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_function_tool("shell")],
            }
        ],
        optional_params={"thinking": {"type": "disabled"}},
        litellm_params={},
        headers={},
    )

    assert "reasoning_content" not in body["messages"][0]


def test_thinking_mode_uses_deployment_placeholder_only_for_tool_history():
    context = build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "placeholder"})
    assert context is not None
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_function_tool("shell")],
            },
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == " "


@pytest.mark.parametrize("missing_reasoning", [None, "", "   "])
def test_deployment_can_omit_missing_reasoning_without_changing_real_history(missing_reasoning: str | None) -> None:
    context: Final = build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "omit"})
    assert context is not None
    assert context.missing_reasoning == "omit"
    messages: Final[list[AllMessageValues]] = [
        {"role": "user", "content": "Use both tools"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Keep the original reasoning exactly.",
            "tool_calls": [_function_tool_call("first")],
        },
        {"role": "tool", "tool_call_id": "call_first", "content": "First result"},
        {
            "role": "assistant",
            "content": "<analysis>Literal XML content</analysis>",
            "reasoning_content": missing_reasoning,
            "tool_calls": [_function_tool_call("second")],
        },
        {"role": "tool", "tool_call_id": "call_second", "content": "Second result"},
    ]
    original: Final = deepcopy(messages)
    body: Final = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=messages,
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == "Keep the original reasoning exactly."
    assert "reasoning_content" not in body["messages"][3]
    assert body["messages"][3]["content"] == "<analysis>Literal XML content</analysis>"
    assert body["messages"][3]["tool_calls"] == messages[3]["tool_calls"]
    assert messages == original


@pytest.mark.asyncio
async def test_async_deployment_omit_policy_keeps_missing_reasoning_absent() -> None:
    context: Final = build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "omit"})
    body: Final = await DeepSeekChatConfig().async_transform_request(
        model="deepseek-reasoner",
        messages=[{"role": "assistant", "content": None, "tool_calls": [_function_tool_call("shell")]}],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert "reasoning_content" not in body["messages"][0]
    assert body["messages"][0]["tool_calls"] == [_function_tool_call("shell")]


def test_deployment_error_policy_rejects_incomplete_reasoning_before_dispatch() -> None:
    context: Final = build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "error"})
    assert context is not None
    assert context.missing_reasoning == "error"
    with pytest.raises(litellm.BadRequestError, match="requires non-empty reasoning_content"):
        DeepSeekChatConfig().transform_request(
            model="deepseek-reasoner",
            messages=[{"role": "assistant", "content": None, "tool_calls": [_function_tool_call("shell")]}],
            optional_params={"thinking": {"type": "enabled"}},
            litellm_params={"_litellm_deployment_protocol_context": context},
            headers={},
        )


@pytest.mark.parametrize("content", [None, ""])
def test_thinking_mode_promotes_scalar_sidecar_reasoning_without_serializing_sidecar(content):
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {
                "role": "assistant",
                "content": content,
                "thinking_blocks": [{"type": "thinking", "thinking": "Use the tool."}],
                "tool_calls": [_function_tool("shell")],
            }
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["reasoning_content"] == "Use the tool."
    assert "thinking_blocks" not in body["messages"][0]


def test_thinking_mode_promotes_provider_reasoning_without_serializing_internal_fields():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "provider_specific_fields": {
                    "reasoning_content": "Use the tool.",
                    "internal_trace": "must not reach provider",
                },
                "tool_calls": [_function_tool("shell")],
            }
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["reasoning_content"] == "Use the tool."
    assert "provider_specific_fields" not in body["messages"][0]


def test_thinking_mode_rejects_redacted_tool_history_even_with_placeholder_policy():
    context = build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "placeholder"})
    assert context is not None
    with pytest.raises(litellm.BadRequestError, match="cannot replay redacted thinking"):
        DeepSeekChatConfig().transform_request(
            model="deepseek-reasoner",
            messages=[
                {"role": "user", "content": "Use the tool"},
                {
                    "role": "assistant",
                    "content": [{"type": "redacted_thinking", "data": "encrypted"}],
                    "tool_calls": [_function_tool("shell")],
                },
            ],
            optional_params={"thinking": {"type": "enabled"}},
            litellm_params={"_litellm_deployment_protocol_context": context},
            headers={},
        )


def test_thinking_mode_drops_redacted_tool_history_when_canonical_reasoning_exists():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": [{"type": "redacted_thinking", "data": "encrypted"}],
                "reasoning_content": "real reasoning",
                "tool_calls": [_function_tool("shell")],
            },
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == "real reasoning"
    assert body["messages"][1]["content"] is None


def test_chat_drops_non_tool_anthropic_thinking_blocks():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-v4-pro",
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Prior reasoning"},
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {"type": "text", "text": "Visible answer"},
                ],
            }
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][0]["reasoning_content"] == "Prior reasoning"
    assert body["messages"][0]["content"] == "Visible answer"


def test_chat_rejects_redacted_only_history_before_http():
    with pytest.raises(litellm.BadRequestError, match="cannot replay redacted-only thinking") as error:
        DeepSeekChatConfig().transform_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ],
            optional_params={},
            litellm_params={},
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


@pytest.mark.parametrize("content", [None, ""])
def test_chat_rejects_scalar_redacted_only_history_before_http(content):
    with pytest.raises(litellm.BadRequestError, match="cannot replay redacted-only thinking") as error:
        DeepSeekChatConfig().transform_request(
            model="deepseek-v4-pro",
            messages=[
                {
                    "role": "assistant",
                    "content": content,
                    "thinking_blocks": [{"type": "redacted_thinking", "data": "encrypted"}],
                }
            ],
            optional_params={},
            litellm_params={},
            headers={},
        )

    assert getattr(error.value, "_litellm_disable_fallbacks", False) is False


def test_thinking_mode_replaces_whitespace_reasoning_with_placeholder():
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": " \t ",
                "tool_calls": [_function_tool("shell")],
            },
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == " "


@pytest.mark.asyncio
async def test_async_thinking_mode_backfills_missing_reasoning_for_tool_history(deepseek_always_on_models):
    tool_call = _function_tool_call("shell")
    body = await DeepSeekChatConfig().async_transform_request(
        model="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            },
        ],
        optional_params={},
        litellm_params={},
        headers={},
    )

    assert body["messages"][1]["reasoning_content"] == " "
    assert body["messages"][1]["tool_calls"] == [tool_call]


def test_placeholder_warning_is_aggregated_and_history_is_immutable(caplog):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_function_tool_call("shell")],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_function_tool_call("apply_patch")],
        },
    ]
    original_messages = deepcopy(messages)
    caplog.set_level("WARNING", logger=litellm.verbose_logger.name)

    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=messages,
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={},
        headers={},
    )

    warnings = [record.getMessage() for record in caplog.records if "single-space placeholder" in record.getMessage()]
    assert len(warnings) == 1
    assert "2 assistant message(s)" in warnings[0]
    assert sum(message.get("reasoning_content") == " " for message in body["messages"]) == 2
    assert messages == original_messages


def test_deployment_tool_thinking_legacy_field_does_not_disable_thinking():
    context = build_deployment_protocol_context(
        {
            "deepseek_anthropic_tool_thinking": "disabled",
        }
    )
    assert context is not None
    body = DeepSeekChatConfig().transform_request(
        model="deepseek-reasoner",
        messages=[
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_function_tool("shell")],
            },
        ],
        optional_params={"thinking": {"type": "enabled"}},
        litellm_params={"_litellm_deployment_protocol_context": context},
        headers={},
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["messages"][1]["reasoning_content"] == " "


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_disabled_thinking_strips_all_reasoning_carriers(is_async):
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Inline reasoning"},
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "I will use the tool."},
            ],
            "thinking_blocks": [{"type": "thinking", "thinking": "Sidecar reasoning"}],
            "reasoning_content": "Canonical reasoning",
            "reasoning": "Foreign reasoning",
            "reasoning_items": [{"type": "reasoning", "id": "reasoning_123"}],
            "thinking": "Foreign thinking",
            "provider_specific_fields": {"reasoning_content": "Provider reasoning"},
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "index": 0,
                    "reasoning_content": "Foreign tool reasoning",
                    "provider_specific_fields": {"thought_signature": "foreign"},
                    "function": {
                        "name": "shell",
                        "arguments": "{}",
                        "thinking": "Foreign function reasoning",
                        "provider_specific_fields": {"thought_signature": "foreign"},
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_function_tool("apply_patch")],
        },
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": "Done",
            "reasoning_content": "Foreign tool result reasoning",
            "thinking_blocks": [{"type": "thinking", "thinking": "Foreign reasoning"}],
            "provider_specific_fields": {"thought_signature": "foreign"},
        },
    ]
    config = DeepSeekChatConfig()

    body = (
        await config.async_transform_request(
            model="deepseek-reasoner",
            messages=messages,
            optional_params={"thinking": {"type": "disabled"}},
            litellm_params={},
            headers={},
        )
        if is_async
        else config.transform_request(
            model="deepseek-reasoner",
            messages=messages,
            optional_params={"thinking": {"type": "disabled"}},
            litellm_params={},
            headers={},
        )
    )

    assert body["messages"][0]["content"] == "I will use the tool."
    assert all("reasoning_content" not in message for message in body["messages"])
    assert all("reasoning" not in message for message in body["messages"])
    assert all("reasoning_items" not in message for message in body["messages"])
    assert all("thinking" not in message for message in body["messages"])
    assert all("thinking_blocks" not in message for message in body["messages"])
    assert all("provider_specific_fields" not in message for message in body["messages"])
    assert body["messages"][0]["tool_calls"] == [
        {
            "id": "call_123",
            "type": "function",
            "function": {"name": "shell", "arguments": "{}"},
        }
    ]
    assert body["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_123",
        "content": "Done",
    }
    assert messages[0]["reasoning_content"] == "Canonical reasoning"
    assert messages[0]["content"][0]["type"] == "thinking"
    assert messages[0]["tool_calls"][0]["provider_specific_fields"] == {"thought_signature": "foreign"}
