import litellm
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.llms.deepseek.messages.transformation import (
    DeepSeekAnthropicMessagesConfig,
)
from litellm.llms.deepseek.anthropic_protocol import (
    DeepSeekProtocolError,
    compile_deepseek_anthropic_history,
)
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
        == "https://api.deepseek.com/anthropic/v1/messages"
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

    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={
            "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
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

    assert request["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "I should call the tool.",
    }
    assert messages[1]["content"][0]["signature"] == "sig"
    assert request["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert request["tools"][0] == {
        "name": "get_weather",
        "description": "Get weather",
        "input_schema": {"type": "object"},
    }
    assert request["tools"][1]["type"] == "web_search_20260209"


def test_deepseek_anthropic_messages_rejects_suffix_over_router_budget():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason"},
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "value"}]},
    ]

    try:
        config.transform_anthropic_messages_request(
            model="deepseek-v4-pro",
            messages=messages,
            anthropic_messages_optional_request_params={
                "max_tokens": 32,
                "thinking": {"type": "enabled"},
                "_deepseek_reasoning_suffix_token_budget": 0,
            },
            litellm_params=GenericLiteLLMParams(),
            headers={},
        )
    except DeepSeekProtocolError as error:
        assert error.code == "reasoning_history_context_exhausted"
    else:
        raise AssertionError("suffix over the router budget must not reach the provider")


def test_deepseek_canonical_history_preserves_parallel_tool_reasoning_and_digest():
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason", "signature": "must-not-leak"},
                {"type": "tool_use", "id": "call-a", "name": "a", "input": {}},
                {"type": "tool_use", "id": "call-b", "name": "b", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-b", "content": "b"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "a"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "follow up"}, {"type": "text", "text": "done"}]},
        {"role": "user", "content": "continue"},
    ]

    compiled = compile_deepseek_anthropic_history(messages)

    assert compiled.history_reasoning_required is True
    assert compiled.suffix is not None
    assert compiled.suffix.call_ids == ("call-a", "call-b")
    assert compiled.suffix.digest
    assert compiled.messages[1]["content"][0] == {"type": "thinking", "thinking": "reason"}
    assert messages[1]["content"][0]["signature"] == "must-not-leak"


def test_deepseek_canonical_history_rejects_missing_reasoning_redaction_and_disabled_mode():
    base = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-a", "name": "a", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "a"}]},
    ]

    for thinking, expected in ((None, "reasoning_history_missing"), ({"type": "disabled"}, "reasoning_history_missing")):
        try:
            compile_deepseek_anthropic_history(base, thinking)
        except DeepSeekProtocolError as error:
            assert error.code == expected
        else:
            raise AssertionError("missing reasoning must fail")
    redacted = [
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "tool_use", "id": "call-a", "name": "a", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "a"}]},
    ]
    try:
        compile_deepseek_anthropic_history(redacted)
    except DeepSeekProtocolError as error:
        assert error.code == "reasoning_history_unrecoverable"
    else:
        raise AssertionError("redacted tool history must fail")


def test_deepseek_canonical_history_rejects_bad_graph_digest_and_budget():
    complete = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason"},
                {"type": "tool_use", "id": "call-a", "name": "a", "input": {}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "a"}]},
    ]
    compiled = compile_deepseek_anthropic_history(complete)
    assert compiled.suffix is not None
    cases = (
        ({"version": 1, "digest": "incorrect"}, "reasoning_history_unrecoverable"),
        (None, "reasoning_history_context_exhausted"),
    )
    for manifest, expected in cases:
        try:
            compile_deepseek_anthropic_history(
                complete,
                manifest=manifest,
                max_suffix_tokens=0 if manifest is None else None,
            )
        except DeepSeekProtocolError as error:
            assert error.code == expected
        else:
            raise AssertionError("invalid suffix must fail")
    orphan = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}]}]
    try:
        compile_deepseek_anthropic_history(orphan)
    except DeepSeekProtocolError as error:
        assert error.code == "tool_result_orphaned"
    else:
        raise AssertionError("orphaned tool result must fail")
    consecutive = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "one"},
                {"type": "tool_use", "id": "call-a", "name": "a", "input": {}},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "two"},
                {"type": "tool_use", "id": "call-b", "name": "b", "input": {}},
            ],
        },
    ]
    try:
        compile_deepseek_anthropic_history(consecutive)
    except DeepSeekProtocolError as error:
        assert error.code == "tool_history_incomplete"
    else:
        raise AssertionError("consecutive unfinished tool calls must fail")


def test_deepseek_messages_transform_compiles_unsigned_thinking_without_mutating_history():
    config = DeepSeekAnthropicMessagesConfig()
    messages = [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "reason", "signature": "x"}]},
        {"role": "user", "content": "continue"},
    ]

    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=messages,
        anthropic_messages_optional_request_params={"max_tokens": 32, "thinking": {"type": "enabled"}},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["messages"][0]["content"] == [{"type": "thinking", "thinking": "reason"}]
    assert messages[0]["content"][0]["signature"] == "x"


def test_deepseek_messages_transform_disabled_omits_optional_reasoning_but_not_tool_history():
    config = DeepSeekAnthropicMessagesConfig()
    request = config.transform_anthropic_messages_request(
        model="deepseek-v4-pro",
        messages=[{"role": "assistant", "content": [{"type": "thinking", "thinking": "optional"}]}],
        anthropic_messages_optional_request_params={"max_tokens": 32, "thinking": {"type": "disabled"}},
        litellm_params=GenericLiteLLMParams(),
        headers={},
    )

    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"][0]["content"] == []
