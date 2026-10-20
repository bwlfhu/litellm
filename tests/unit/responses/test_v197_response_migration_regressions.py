import json

from litellm.completion_extras.litellm_responses_transformation.transformation import (
    LiteLLMResponsesTransformationHandler,
    OpenAiResponsesToChatCompletionStreamIterator,
)
from litellm.llms.chatgpt.chat.streaming_utils import ChatGPTToolCallNormalizer
from litellm.responses.litellm_completion_transformation.transformation import (
    LiteLLMCompletionResponsesConfig,
)
from litellm.responses.utils import ResponsesAPIRequestUtils


def test_namespace_tools_round_trip_to_chat_and_back():
    tools = [
        {
            "type": "namespace",
            "name": "repo",
            "description": "Repository tools",
            "tools": [
                {
                    "type": "function",
                    "name": "search",
                    "description": "Search files",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    ]

    chat_tools, _ = LiteLLMCompletionResponsesConfig.transform_responses_api_tools_to_chat_completion_tools(tools)
    assert chat_tools[0]["function"]["name"] == "repo__search"

    responses_tools = LiteLLMCompletionResponsesConfig.transform_chat_completion_tool_params_to_responses_api_tools(
        chat_tools
    )
    assert responses_tools[0]["name"] == "repo__search"


def test_replayed_openai_items_drop_foreign_ids_without_mutating_input():
    request_input = [
        {"type": "reasoning", "id": "item_foreign", "status": "completed", "summary": []},
        {"type": "function_call", "id": "tooluse_foreign", "call_id": "tooluse_call"},
        {"type": "message", "id": "item_message", "status": "completed", "content": []},
    ]

    normalized = ResponsesAPIRequestUtils._normalize_replayed_item_ids_in_input(
        request_input=request_input,
        model="gpt-5.4",
        custom_llm_provider="openai",
    )

    assert "id" not in normalized[0]
    assert normalized[1]["id"] == "fc_foreign"
    assert "id" not in normalized[2]
    assert all("status" not in item for item in normalized)
    assert request_input[0]["id"] == "item_foreign"


def test_done_only_parallel_tool_arguments_survive_stream_normalization():
    iterator = OpenAiResponsesToChatCompletionStreamIterator(streaming_response=iter(()), sync_stream=True)
    chunks = [
        iterator.chunk_parser(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "function_call", "call_id": "call_1", "name": "first"},
            }
        ),
        iterator.chunk_parser(
            {
                "type": "response.output_item.added",
                "output_index": 3,
                "item": {"type": "function_call", "call_id": "call_2", "name": "second"},
            }
        ),
        iterator.chunk_parser(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "arguments": '{"first":true}',
            }
        ),
        iterator.chunk_parser(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 3,
                "arguments": '{"second":true}',
            }
        ),
        iterator.chunk_parser({"type": "response.completed", "response": {"status": "completed"}}),
    ]

    from litellm import stream_chunk_builder

    rebuilt = stream_chunk_builder(list(ChatGPTToolCallNormalizer(iter(chunks))))
    assert rebuilt is not None
    assert [call.function.arguments for call in rebuilt.choices[0].message.tool_calls] == [
        '{"first":true}',
        '{"second":true}',
    ]
    assert rebuilt.choices[0].finish_reason == "tool_calls"


def test_response_tool_cache_key_isolated_by_response_id():
    first = LiteLLMCompletionResponsesConfig._tool_call_cache_key("resp_a", "call_1")
    second = LiteLLMCompletionResponsesConfig._tool_call_cache_key("resp_b", "call_1")
    assert first != second


def test_raw_response_message_and_tool_call_share_one_choice():
    handler = LiteLLMResponsesTransformationHandler()
    output = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking weather"}],
        },
        {
            "type": "reasoning",
            "id": "rs_raw",
            "summary": [{"type": "summary_text", "text": "choose city"}],
            "encrypted_content": "encrypted-raw",
        },
        {
            "type": "function_call",
            "id": "fc_raw",
            "call_id": "call_raw",
            "name": "get_weather",
            "arguments": json.dumps({"city": "Paris"}, separators=(",", ":")),
        },
    ]

    choices = handler._convert_response_output_to_choices(
        output,
        handle_raw_dict_callback=handler._handle_raw_dict_response_item,
    )
    assert len(choices) == 1
    assert choices[0].finish_reason == "tool_calls"
    assert choices[0].message.tool_calls[0].function.name == "get_weather"
