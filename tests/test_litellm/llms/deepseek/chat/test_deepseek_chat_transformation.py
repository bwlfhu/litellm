from copy import deepcopy

import pytest

import litellm
from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig
from litellm.router_protocol import _build_deployment_protocol_context


@pytest.fixture
def deepseek_vision_models():
    model_names = (
        "deepseek-v4-flash-vision-exp",
        "deepseek/deepseek-v4-flash-vision-exp",
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


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-v4-flash"])
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
    context = _build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "placeholder"})
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
    context = _build_deployment_protocol_context({"deepseek_anthropic_missing_reasoning": "placeholder"})
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
    context = _build_deployment_protocol_context(
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
