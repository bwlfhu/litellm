import pytest
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath("../.."))

from litellm.utils import log_tool_request_shape, validate_chat_completion_tool_choice


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
        litellm.completion(
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
    """Test Cursor IDE format: {"type": "auto"} -> {"type": "auto"}."""
    assert validate_chat_completion_tool_choice({"type": "auto"}) == {"type": "auto"}
    assert validate_chat_completion_tool_choice({"type": "none"}) == {"type": "none"}
    assert validate_chat_completion_tool_choice({"type": "required"}) == {"type": "required"}


def test_validate_tool_choice_invalid_dict():
    """Test that invalid dict formats raise exceptions."""
    # Missing both type and function
    with pytest.raises(Exception) as exc_info:
        validate_chat_completion_tool_choice({})
    assert "Invalid tool choice" in str(exc_info.value)

    # Invalid type value
    with pytest.raises(Exception) as exc_info:
        validate_chat_completion_tool_choice({"type": "invalid"})
    assert "Invalid tool choice" in str(exc_info.value)

    # Has type but missing function when type is "function"
    with pytest.raises(Exception) as exc_info:
        validate_chat_completion_tool_choice({"type": "function"})
    assert "Invalid tool choice" in str(exc_info.value)


def test_validate_tool_choice_invalid_type():
    """Test that invalid types raise exceptions."""
    with pytest.raises(Exception) as exc_info:
        validate_chat_completion_tool_choice(123)
    assert "Got=<class 'int'>" in str(exc_info.value)

    with pytest.raises(Exception) as exc_info:
        validate_chat_completion_tool_choice([])
    assert "Got=<class 'list'>" in str(exc_info.value)
