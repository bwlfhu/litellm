"""
Tests for normalizing Responses API function_call_output into chat tool messages.

This is important for Gemini/Vertex, which expects tool results to be represented
as tool/function response parts; if the tool output is passed as a list of input_* parts,
we normalize it to text/image blocks or a string.
"""

from litellm.responses.litellm_completion_transformation.transformation import (
    TOOL_CALLS_CACHE,
    LiteLLMCompletionResponsesConfig,
)
from litellm.types.utils import ModelResponse


def test_function_call_output_list_input_text_is_converted_to_tool_string_content():
    out = LiteLLMCompletionResponsesConfig._transform_responses_api_tool_call_output_to_chat_completion_message(
        tool_call_output={
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [
                {"type": "input_text", "text": "hello"},
                {"type": "input_text", "text": " world"},
            ],
        }
    )

    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_1"
    assert msg["content"] == "hello world"


def test_function_call_output_string_passthrough():
    out = LiteLLMCompletionResponsesConfig._transform_responses_api_tool_call_output_to_chat_completion_message(
        tool_call_output={
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok":true}',
        }
    )
    assert len(out) == 1
    assert out[0]["content"] == '{"ok":true}'


def test_function_call_output_uses_tool_call_from_matching_response_when_call_id_is_reused():
    call_id = "call_shared"
    earlier_response_id = "resp_earlier"
    later_response_id = "resp_later"

    def response(response_id: str, tool_name: str) -> ModelResponse:
        return ModelResponse(
            id=response_id,
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": tool_name, "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
        )

    earlier_cache_key = LiteLLMCompletionResponsesConfig._tool_call_cache_key(
        response_id=earlier_response_id,
        call_id=call_id,
    )
    later_cache_key = LiteLLMCompletionResponsesConfig._tool_call_cache_key(
        response_id=later_response_id,
        call_id=call_id,
    )

    try:
        LiteLLMCompletionResponsesConfig.transform_chat_completion_tools_to_responses_tools(
            chat_completion_response=response(earlier_response_id, "earlier_tool")
        )
        LiteLLMCompletionResponsesConfig.transform_chat_completion_tools_to_responses_tools(
            chat_completion_response=response(later_response_id, "later_tool")
        )
        out = LiteLLMCompletionResponsesConfig.transform_responses_api_input_to_messages(
            input=[
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "result",
                }
            ],
            responses_api_request={"previous_response_id": earlier_response_id},
        )
    finally:
        TOOL_CALLS_CACHE.delete_cache(key=earlier_cache_key)
        TOOL_CALLS_CACHE.delete_cache(key=later_cache_key)
        TOOL_CALLS_CACHE.delete_cache(key=call_id)

    assert len(out) == 2
    assert out[0]["role"] == "assistant"
    assert out[0]["tool_calls"][0]["function"]["name"] == "earlier_tool"
    assert out[1]["role"] == "tool"
