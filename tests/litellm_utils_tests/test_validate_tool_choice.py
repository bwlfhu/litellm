import pytest


import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch


from litellm.utils import (
    log_anthropic_response_shape,
    log_tool_request_shape,
    validate_chat_completion_tool_choice,
)


def test_log_tool_request_shape_warns_without_tools(caplog):
    tools = None
    log_tool_request_shape(
        tools=tools,
        tool_choice="auto",
        endpoint="/v1/chat/completions",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="received",
    )

    assert "tool_choice without tools" in caplog.text
    assert "description" not in caplog.text


def test_log_tool_request_shape_warns_for_empty_tools(caplog):
    log_tool_request_shape(
        tools=[],
        tool_choice={"type": "required"},
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="normalized",
    )

    assert "tool_choice without tools" in caplog.text


def test_log_tool_request_shape_warns_for_empty_mapping(caplog):
    log_tool_request_shape(
        tools={},
        tool_choice="auto",
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="received",
    )

    assert "tool_choice without tools" in caplog.text


def test_log_tool_request_shape_debugs_redacted_summary(monkeypatch):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "secret_tool",
                "description": "secret description",
                "parameters": {"type": "object", "properties": {"token": {"type": "string"}}},
            },
        },
        {"type": "web_search"},
    ]
    debug_messages = []
    monkeypatch.setattr(
        "litellm.utils.verbose_logger.debug",
        lambda message, summary: debug_messages.append(message % summary),
    )
    log_tool_request_shape(
        tools=tools,
        tool_choice="auto",
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="normalized",
    )

    assert len(debug_messages) == 1
    debug_message = debug_messages[0]
    assert "Tool request shape:" in debug_message
    assert "function" in debug_message
    assert "web_search" in debug_message
    assert "secret_tool" not in debug_message
    assert "secret description" not in debug_message
    assert "token" not in debug_message


def test_log_tool_request_shape_infos_stable_redacted_hash(caplog, monkeypatch):
    monkeypatch.setenv("LITELLM_TOOL_DIAGNOSTICS", "true")
    caplog.set_level("INFO")
    first_tools = [
        {
            "name": "private_tool_name",
            "input_schema": {
                "properties": {"secret_argument": {"type": "string"}},
                "type": "object",
            },
        }
    ]
    second_tools = [
        {
            "input_schema": {
                "type": "object",
                "properties": {"secret_argument": {"type": "string"}},
            },
            "name": "private_tool_name",
        }
    ]

    log_tool_request_shape(
        tools=first_tools,
        tool_choice=None,
        endpoint="/v1/messages",
        model="claude-test",
        custom_llm_provider="anthropic",
        phase="received",
        call_id="call-1",
        log_when_present=True,
    )
    first_log = caplog.text
    caplog.clear()
    log_tool_request_shape(
        tools=second_tools,
        tool_choice=None,
        endpoint="/v1/messages",
        model="claude-test",
        custom_llm_provider="anthropic",
        phase="provider_dispatch",
        call_id="call-1",
        log_when_present=True,
    )
    second_log = caplog.text

    first_hash = first_log.split("'tool_schema_hash': '", 1)[1].split("'", 1)[0]
    second_hash = second_log.split("'tool_schema_hash': '", 1)[1].split("'", 1)[0]
    assert first_hash == second_hash
    assert len(first_hash) == 16
    assert "private_tool_name" not in first_log
    assert "secret_argument" not in first_log


def test_log_anthropic_response_shape_is_redacted(caplog, monkeypatch):
    monkeypatch.setenv("LITELLM_TOOL_DIAGNOSTICS", "true")
    caplog.set_level("INFO")
    log_anthropic_response_shape(
        content=(
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "tool_use", "name": "private_tool", "input": {"secret": "value"}},
            {"type": "text", "text": "private response"},
        ),
        stop_reason="tool_use",
        model="claude-test",
        custom_llm_provider="anthropic",
        call_id="call-2",
        stream=True,
    )

    assert "'content_block_count': 3" in caplog.text
    assert "'tool_use_count': 1" in caplog.text
    assert "'stop_reason': 'tool_use'" in caplog.text
    assert "private reasoning" not in caplog.text
    assert "private_tool" not in caplog.text
    assert "private response" not in caplog.text


def test_tool_diagnostics_redacts_untrusted_enum_values(caplog, monkeypatch):
    monkeypatch.setenv("LITELLM_TOOL_DIAGNOSTICS", "true")
    caplog.set_level("INFO")

    log_tool_request_shape(
        tools={"type": "PRIVATE_PROMPT_FRAGMENT", "name": "private_tool"},
        tool_choice=None,
        endpoint="/v1/messages",
        model="claude-test",
        custom_llm_provider="anthropic",
        phase="received",
        log_when_present=True,
    )
    log_anthropic_response_shape(
        content=({"type": "PRIVATE_RESPONSE_FRAGMENT", "text": "private response"},),
        stop_reason="PRIVATE_STOP_FRAGMENT",
        model="claude-test",
        custom_llm_provider="anthropic",
        call_id="call-redacted",
        stream=True,
    )

    assert "PRIVATE_" not in caplog.text
    assert "private_tool" not in caplog.text
    assert "private response" not in caplog.text
    assert "'tool_types': ('other',)" in caplog.text
    assert "'content_block_types': ('other',)" in caplog.text
    assert "'stop_reason': 'other'" in caplog.text


def test_disabled_tool_diagnostics_skip_schema_hash(caplog):
    with patch("litellm.utils._tool_schema_hash", side_effect=AssertionError("hash must not run")):
        log_tool_request_shape(
            tools=[{"type": "function"}],
            tool_choice=None,
            endpoint="/v1/messages",
            model="claude-test",
            custom_llm_provider="anthropic",
            phase="received",
            log_when_present=True,
        )

    assert "Tool request shape" not in caplog.text


def test_tool_diagnostic_hash_failure_does_not_interrupt_request(caplog, monkeypatch):
    from pydantic import BaseModel

    class BrokenTool(BaseModel):
        def model_dump(self, *args, **kwargs):
            raise RuntimeError("PRIVATE_SERIALIZATION_FRAGMENT")

    monkeypatch.setenv("LITELLM_TOOL_DIAGNOSTICS", "true")
    caplog.set_level("INFO")

    log_tool_request_shape(
        tools=[BrokenTool()],
        tool_choice=None,
        endpoint="/v1/messages",
        model="claude-test",
        custom_llm_provider="anthropic",
        phase="received",
        log_when_present=True,
    )

    assert "'tool_schema_hash': None" in caplog.text
    assert "PRIVATE_SERIALIZATION_FRAGMENT" not in caplog.text


def test_log_tool_request_shape_does_not_warn_for_normal_no_tool_request(caplog):
    log_tool_request_shape(
        tools=None,
        tool_choice=None,
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="received",
    )

    assert "tool_choice without tools" not in caplog.text


def test_log_tool_request_shape_warns_for_malformed_function_schema(caplog):
    log_tool_request_shape(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "list_published_schemas",
                    "parameters": {"type": "function"},
                },
            }
        ],
        tool_choice="auto",
        endpoint="/v1/chat/completions",
        model="gpt-5.5",
        custom_llm_provider="openai",
        phase="received",
        call_id="req-1",
    )

    assert "malformed schemas" in caplog.text
    assert "list_published_schemas" not in caplog.text
    assert "req-1" in caplog.text
    assert "properties" not in caplog.text
    assert "'tool_index': 0" in caplog.text
    assert "'root_type_kind': 'string'" in caplog.text


def test_log_tool_request_shape_warns_for_malformed_flat_responses_function_schema(caplog):
    log_tool_request_shape(
        tools=[{"type": "function", "name": "flat_tool", "parameters": {"type": "function"}}],
        tool_choice="auto",
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="normalized",
    )

    assert "malformed schemas" in caplog.text
    assert "flat_tool" not in caplog.text


def test_log_tool_request_shape_does_not_raise_for_broken_mapping(caplog):
    class BrokenMapping(dict):
        def get(self, key, default=None):
            raise RuntimeError("no access")

        def __len__(self):
            raise RuntimeError("no length")

    log_tool_request_shape(
        tools=[BrokenMapping()],
        tool_choice="auto",
        endpoint="/v1/messages",
        model="claude-test",
        custom_llm_provider="anthropic",
        phase="received",
    )

    assert "malformed schemas" not in caplog.text


def test_log_tool_request_shape_can_warn_for_missing_tools_without_tool_choice(caplog):
    log_tool_request_shape(
        tools=None,
        tool_choice=None,
        endpoint="/v1/messages",
        model="claude-sonnet-5",
        custom_llm_provider="anthropic",
        phase="received",
        warn_when_missing=True,
    )

    assert "contains no tools" in caplog.text


def test_log_tool_request_shape_does_not_consume_unknown_iterable(caplog):
    consumed = False

    def tool_generator():
        nonlocal consumed
        consumed = True
        yield {"type": "function"}

    tools = tool_generator()
    log_tool_request_shape(
        tools=tools,
        tool_choice="auto",
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="received",
    )

    assert consumed is False
    assert "tool_choice without tools" not in caplog.text


def test_log_tool_request_shape_does_not_consume_custom_sized_iterable(caplog):
    class SizedIterable:
        consumed = False

        def __len__(self):
            return 1

        def __iter__(self):
            self.consumed = True
            yield {"type": "function"}

    tools = SizedIterable()
    log_tool_request_shape(
        tools=tools,
        tool_choice="auto",
        endpoint="/v1/responses",
        model="gpt-test",
        custom_llm_provider="openai",
        phase="received",
    )

    assert tools.consumed is False
    assert "tool_choice without tools" not in caplog.text


def test_completion_entry_logs_tool_shape_without_changing_request():
    import litellm

    with patch("litellm.main.log_tool_request_shape") as log:
        response = litellm.completion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            tool_choice="auto",
            mock_response="ok",
            custom_llm_provider="openai",
        )

    assert [call.kwargs["phase"] for call in log.call_args_list] == ["received", "normalized"]
    assert all(call.kwargs["tools"] == [] for call in log.call_args_list)
    assert all(call.kwargs["custom_llm_provider"] == "openai" for call in log.call_args_list)
    assert response.choices[0].message.content == "ok"


def test_responses_entry_logs_tool_shape_without_changing_request():
    import litellm

    sentinel = object()
    with (
        patch("litellm.responses.main.log_tool_request_shape") as log,
        patch(
            "litellm.responses.main.ProviderConfigManager.get_provider_responses_api_config",
            return_value=None,
        ),
        patch(
            "litellm.responses.main.litellm_completion_transformation_handler.response_api_handler",
            return_value=sentinel,
        ),
    ):
        result = litellm.responses(
            model="custom/test",
            input="hello",
            tools=[],
            tool_choice="auto",
            custom_llm_provider="custom",
        )

    assert result is sentinel
    assert [call.kwargs["phase"] for call in log.call_args_list] == ["received", "normalized"]
    assert log.call_args_list[-1].kwargs["model"] == "test"


@pytest.mark.asyncio
async def test_async_entries_log_initial_and_normalized_shapes():
    import litellm

    with patch("litellm.main.log_tool_request_shape") as completion_log:
        await litellm.acompletion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "tool"}}],
            tool_choice="auto",
            mock_response="ok",
        )

    assert [call.kwargs["phase"] for call in completion_log.call_args_list] == ["received", "normalized"]

    sentinel = object()
    with (
        patch("litellm.responses.main.log_tool_request_shape") as responses_log,
        patch(
            "litellm.responses.main.ProviderConfigManager.get_provider_responses_api_config",
            return_value=None,
        ),
        patch(
            "litellm.responses.main.litellm_completion_transformation_handler.response_api_handler",
            return_value=sentinel,
        ),
    ):
        result = await litellm.aresponses(
            model="custom/test",
            input="hello",
            tools=[],
            tool_choice="auto",
            custom_llm_provider="custom",
        )

    assert result is sentinel
    assert [call.kwargs["phase"] for call in responses_log.call_args_list] == ["received", "normalized"]


@pytest.mark.asyncio
async def test_async_prompt_hook_tool_filter_is_visible_in_phase_logs():
    import litellm
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

    logging_obj = MagicMock()
    logging_obj.__class__ = LiteLLMLoggingObj
    logging_obj.should_run_prompt_management_hooks.return_value = True
    logging_obj.model_call_details = {}
    logging_obj.get_chat_completion_prompt.return_value = (
        "openai/gpt-4o-mini",
        [{"role": "user", "content": "hello"}],
        {},
    )
    logging_obj.async_failure_handler = AsyncMock()

    async def clear_tools(**kwargs):
        kwargs["tools"].clear()
        return kwargs["model"], kwargs["messages"], {}

    logging_obj.async_get_chat_completion_prompt = AsyncMock(side_effect=clear_tools)
    events = []

    def record_event(**kwargs):
        tools = kwargs["tools"]
        snapshot = None if tools is None else tuple(tools)
        events.append((kwargs["phase"], snapshot))

    with patch("litellm.main.log_tool_request_shape", side_effect=record_event):
        await litellm.acompletion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "tool"}}],
            tool_choice="auto",
            litellm_logging_obj=logging_obj,
            prompt_id="filter-tools",
            mock_response="ok",
        )

    assert events[0][0] == "received"
    assert events[0][1] == ({"type": "function", "function": {"name": "tool"}},)
    assert events[-1] == ("normalized", None)


def test_validate_tool_choice_none():
    """Test that None is returned as-is."""
    result = validate_chat_completion_tool_choice(None)
    assert result is None


def test_validate_tool_choice_string():
    """Test that string values are returned as-is."""
    assert validate_chat_completion_tool_choice("auto") == "auto"
    assert validate_chat_completion_tool_choice("none") == "none"
    assert validate_chat_completion_tool_choice("required") == "required"


def test_validate_tool_choice_standard_dict():
    """Test standard OpenAI format with function."""
    tool_choice = {"type": "function", "function": {"name": "my_function"}}
    result = validate_chat_completion_tool_choice(tool_choice)
    assert result == tool_choice


def test_validate_tool_choice_cursor_format():
    """Cursor IDE format {"type": "auto"} is unwrapped to the bare string."""
    assert validate_chat_completion_tool_choice({"type": "auto"}) == "auto"
    assert validate_chat_completion_tool_choice({"type": "none"}) == "none"
    assert validate_chat_completion_tool_choice({"type": "required"}) == "required"


def test_validate_tool_choice_invalid_dict():
    """Test that invalid dict formats raise exceptions."""
    # Missing both type and function
    with pytest.raises(Exception, match="Invalid tool choice, tool_choice=\\{\\}\\. Please ensure") as exc_info:
        validate_chat_completion_tool_choice({})
    assert "Invalid tool choice" in str(exc_info.value)

    # Invalid type value
    with pytest.raises(Exception, match="Invalid tool choice, tool_choice=\\{'type': 'invalid'\\}\\.") as exc_info:
        validate_chat_completion_tool_choice({"type": "invalid"})
    assert "Invalid tool choice" in str(exc_info.value)

    # Has type but missing function when type is "function"
    with pytest.raises(Exception, match="Invalid tool choice, tool_choice=\\{'type': 'function'\\}\\.") as exc_info:
        validate_chat_completion_tool_choice({"type": "function"})
    assert "Invalid tool choice" in str(exc_info.value)


def test_validate_tool_choice_invalid_type():
    """Test that invalid types raise exceptions."""
    with pytest.raises(Exception, match="<class 'int'>\\. Expecting str, or dict\\. Please ensure") as exc_info:
        validate_chat_completion_tool_choice(123)
    assert "Got=<class 'int'>" in str(exc_info.value)

    with pytest.raises(Exception, match="Invalid tool choice, tool_choice=\\[\\]\\. Got=<class 'list'>\\.") as exc_info:
        validate_chat_completion_tool_choice([])
    assert "Got=<class 'list'>" in str(exc_info.value)
