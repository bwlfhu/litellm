import time
import uuid
from typing import Any, Final, cast

import litellm
from litellm.main import stream_chunk_builder
from litellm.responses.litellm_completion_transformation.custom_tools import (
    build_tool_call_item_kwargs,
    extract_custom_tool_names,
    unwrap_custom_tool_arguments,
)
from litellm.responses.litellm_completion_transformation.transformation import (
    LiteLLMCompletionResponsesConfig,
)
from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator
from litellm.responses.utils import ResponsesAPIRequestUtils
from litellm.types.llms.openai import (
    PART_UNION_TYPES,
    BaseLiteLLMOpenAIResponseObject,
    ContentPartAddedEvent,
    ContentPartDoneEvent,
    ContentPartDonePartOutputText,
    ContentPartDonePartReasoningText,
    CustomToolCallInputDeltaEvent,
    CustomToolCallInputDoneEvent,
    FunctionCallArgumentsDeltaEvent,
    FunctionCallArgumentsDoneEvent,
    OutputItemAddedEvent,
    OutputItemDoneEvent,
    OutputTextAnnotationAddedEvent,
    OutputTextDeltaEvent,
    OutputTextDoneEvent,
    ReasoningSummaryPartDoneEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningSummaryTextDoneEvent,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseInProgressEvent,
    ResponseInputParam,
    ResponsesAPIOptionalRequestParams,
    ResponsesAPIResponse,
    ResponsesAPIStreamEvents,
    ResponsesAPIStreamingResponse,
)
from litellm.types.utils import (
    ChatCompletionMessageCustomToolCall,
    ChatCompletionMessageToolCall,
    ModelResponse,
    ModelResponseStream,
    StreamingChoices,
    TextCompletionResponse,
)
from litellm.types.utils import Delta as ChatCompletionDelta


def _output_items_with_id(items: tuple[Any, ...], item_type: str, item_id: str | None) -> tuple[Any, ...]:
    if item_id is None:
        return items

    target_index: Final = next(
        (index for index, item in enumerate(items) if getattr(item, "type", None) == item_type),
        None,
    )
    if target_index is None:
        return items

    return tuple(
        item.model_copy(update={"id": item_id}) if index == target_index else item for index, item in enumerate(items)
    )


class LiteLLMCompletionStreamingIterator(ResponsesAPIStreamingIterator):
    """
    Async iterator for processing streaming responses from the Responses API.
    """

    def __init__(
        self,
        model: str,
        litellm_custom_stream_wrapper: litellm.CustomStreamWrapper,
        request_input: str | ResponseInputParam,
        responses_api_request: ResponsesAPIOptionalRequestParams,
        custom_llm_provider: str | None = None,
        litellm_metadata: dict | None = None,
    ):
        self.model: str = model
        self.litellm_custom_stream_wrapper: litellm.CustomStreamWrapper = litellm_custom_stream_wrapper
        self.request_input: str | ResponseInputParam = request_input
        self.responses_api_request: ResponsesAPIOptionalRequestParams = responses_api_request
        self.custom_llm_provider: str | None = custom_llm_provider
        self.litellm_metadata = litellm_metadata or {}
        self.completed_response: ResponseCompletedEvent | None = None
        wrapper_hidden_params = getattr(litellm_custom_stream_wrapper, "_hidden_params", None)
        self._hidden_params: dict[str, Any] = (
            dict(wrapper_hidden_params) if isinstance(wrapper_hidden_params, dict) else {}
        )
        # Store lightweight dict snapshots for stream_chunk_builder to reduce
        # repeated Pydantic attribute access in end-of-stream assembly.
        self.collected_chat_completion_chunks: list[dict[str, Any]] = []
        self.finished: bool = False
        self.litellm_logging_obj = litellm_custom_stream_wrapper.logging_obj
        self.sent_response_created_event: bool = False
        self.sent_response_in_progress_event: bool = False
        self.sent_output_item_added_event: bool = False
        self.sent_content_part_added_event: bool = False
        self.sent_output_text_done_event: bool = False
        self.sent_output_content_part_done_event: bool = False
        self.sent_output_item_done_event: bool = False
        self.sent_annotation_events: bool = False
        self.litellm_model_response: ModelResponse | TextCompletionResponse | None = None
        self.final_text: str = ""
        self._cached_item_id: str | None = None
        self._current_step_text = ""
        self._current_step_output_index: int = 0
        self._current_reasoning_output_index: int = 0
        self._current_step_finish_reason: str | None = None
        self._completed_message_steps: tuple[tuple[int, BaseLiteLLMOpenAIResponseObject], ...] = ()
        self._completed_reasoning_steps: tuple[tuple[int, BaseLiteLLMOpenAIResponseObject], ...] = ()
        self._completed_tool_steps: tuple[tuple[int, BaseLiteLLMOpenAIResponseObject], ...] = ()
        self._completed_step_tool_calls: tuple[
            ChatCompletionMessageToolCall | ChatCompletionMessageCustomToolCall, ...
        ] = ()
        self._current_step_chunk_start: int = 0
        self._current_step_finalized: bool = False
        self._current_step_annotations: list[BaseLiteLLMOpenAIResponseObject] = []
        self._active_upstream_chunk_id: str | None = None
        self._upstream_step_finished = False
        self._cached_response_id: str | None = None
        self._pending_tool_events: list[BaseLiteLLMOpenAIResponseObject] = []
        self._tool_output_index_by_call_id: dict[str, int] = {}
        self._tool_args_by_call_id: dict[str, str] = {}
        self._tool_fields_by_call_id: dict[str, tuple[str, str | None, str | None]] = {}
        self._tool_call_id_by_index: dict[int, str] = {}
        self._ambiguous_tool_call_indexes: set[int] = set()
        self._next_tool_output_index: int = 1  # output_index=0 reserved for the message item
        self._final_tool_events_queued: bool = False
        self._sequence_number: int = 0
        self._cached_reasoning_item_id: str | None = None
        self._sent_reasoning_summary_text_done_event: bool = False
        self._sent_reasoning_summary_part_done_event: bool = False
        self._reasoning_summary_text: str = ""
        # -- GENERIC RESPONSE-EVENTS PENDING QUEUE as required by fix --
        self._pending_response_events: list[BaseLiteLLMOpenAIResponseObject] = []
        self._reasoning_active = False
        self._reasoning_done_emitted = False
        self._reasoning_item_id: str | None = None
        self._accumulated_reasoning_content_parts: list[str] = []
        self._accumulated_provider_specific_fields: dict[str, Any] = {}
        self._custom_tool_names: set[str] = extract_custom_tool_names(self.responses_api_request.get("tools"))
        self._namespace_tool_names = LiteLLMCompletionResponsesConfig._namespace_tool_name_map(
            self.responses_api_request.get("tools")
        )

    def _get_or_assign_tool_output_index(self, call_id: str) -> int:
        existing: Final = self._tool_output_index_by_call_id.get(call_id)
        if existing is not None:
            return existing
        idx: Final = self._next_tool_output_index
        self._next_tool_output_index += 1
        self._tool_output_index_by_call_id[call_id] = idx
        return idx

    def _allocate_output_index(self) -> int:
        index: Final = self._next_tool_output_index
        self._next_tool_output_index += 1
        return index

    def _normalize_tool_call_index(self, tool_call: object) -> int | None:
        idx_raw: Final = tool_call.get("index") if isinstance(tool_call, dict) else getattr(tool_call, "index", None)
        if idx_raw is None:
            return None
        try:
            return int(idx_raw)
        except (TypeError, ValueError):
            return None

    def _responses_namespace_tool_call_fields(self, function_name: str) -> tuple[str, str | None, str | None]:
        return LiteLLMCompletionResponsesConfig._restore_namespace_tool_name(
            function_name,
            self._namespace_tool_names,
        )

    def _custom_names_for_tool_call(
        self,
        tool_name: str,
        namespace: str | None,
        namespace_tool_type: str | None,
    ) -> set[str]:
        if namespace is None:
            return self._custom_tool_names
        if namespace_tool_type == "custom":
            return {tool_name}
        return set()

    def _resolve_tool_call_fields(
        self,
        call_id: str,
        function_name: str,
    ) -> tuple[str, str | None, str | None]:
        if function_name:
            fields = self._responses_namespace_tool_call_fields(function_name)
            self._tool_fields_by_call_id[call_id] = fields
            return fields
        return self._tool_fields_by_call_id.get(call_id, ("", None, None))

    def _is_reasoning_end(self, chunk):
        if not chunk.choices:
            return False
        delta: Final = chunk.choices[0].delta

        # if this indicates reasoning content, don't consider reasoning ended
        if getattr(delta, "reasoning_content", None) or getattr(delta, "thinking_blocks", None):
            return False
        if hasattr(delta, "thinking_blocks") and delta.thinking_blocks:
            return False

        return delta.content or delta.function_call or delta.tool_calls or chunk.choices[0].finish_reason is not None

    def _queue_tool_call_delta_events(self, tool_calls: object) -> None:
        """
        Convert chat-completions streaming `tool_calls` deltas into Responses API streaming events.

        We emit:
        - response.output_item.added (function_call)
        - response.function_call_arguments.delta (split into smaller chunks to match OpenAI behavior)

        Note: Some providers (like Bedrock) send tool call arguments in one large chunk.
        We split these into smaller deltas to match OpenAI's token-by-token streaming behavior.
        """
        if not isinstance(tool_calls, list):
            return

        for tc in tool_calls:
            tc_index = self._normalize_tool_call_index(tc)
            call_id_raw = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            call_id = ""

            if call_id_raw:
                call_id = str(call_id_raw)
                if tc_index is not None:
                    existing_call_id = self._tool_call_id_by_index.get(tc_index)
                    if existing_call_id is not None and existing_call_id != call_id:
                        # Reusing the same index for multiple call_ids is ambiguous for id-less deltas.
                        # Guard against silent misrouting by disabling index fallback for this index.
                        self._ambiguous_tool_call_indexes.add(tc_index)
                    self._tool_call_id_by_index[tc_index] = call_id
            elif tc_index is not None:
                if tc_index in self._ambiguous_tool_call_indexes:
                    continue
                mapped_call_id = self._tool_call_id_by_index.get(tc_index)
                if mapped_call_id:
                    call_id = mapped_call_id

            if not call_id:
                continue

            fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
            fn_name = ""
            fn_args_delta = ""
            if isinstance(fn, dict):
                fn_name = str(fn.get("name") or "")
                fn_args_delta = str(fn.get("arguments") or "")
            else:
                fn_name = str(getattr(fn, "name", "") or "")
                fn_args_delta = str(getattr(fn, "arguments", "") or "")
            tool_name, tool_namespace, namespace_tool_type = self._resolve_tool_call_fields(call_id, fn_name)
            custom_tool_names = self._custom_names_for_tool_call(
                tool_name,
                tool_namespace,
                namespace_tool_type,
            )

            output_index = self._get_or_assign_tool_output_index(call_id)

            if call_id not in self._tool_args_by_call_id:
                self._tool_args_by_call_id[call_id] = ""
                self._sequence_number += 1
                item_kwargs = build_tool_call_item_kwargs(
                    call_id,
                    tool_name,
                    "",
                    "in_progress",
                    custom_tool_names,
                )
                if tool_namespace:
                    item_kwargs["namespace"] = tool_namespace
                event = OutputItemAddedEvent(
                    type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
                    output_index=output_index,
                    item=BaseLiteLLMOpenAIResponseObject(**item_kwargs),
                    sequence_number=self._sequence_number,
                )
                self._pending_tool_events.append(event)

            if fn_args_delta:
                self._tool_args_by_call_id[call_id] += fn_args_delta

                if tool_name in custom_tool_names:
                    continue

                # Split large argument deltas into smaller chunks to match OpenAI's streaming behavior
                # This is especially important for providers like Bedrock that send complete arguments at once
                chunk_size = 10  # Match typical OpenAI delta size
                for i in range(0, len(fn_args_delta), chunk_size):
                    delta_chunk = fn_args_delta[i : i + chunk_size]
                    self._sequence_number += 1
                    delta_event: BaseLiteLLMOpenAIResponseObject = FunctionCallArgumentsDeltaEvent(
                        type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DELTA,
                        item_id=call_id,
                        output_index=output_index,
                        delta=delta_chunk,
                        sequence_number=self._sequence_number,
                    )
                    self._pending_tool_events.append(delta_event)

    def _queue_final_tool_call_done_events(self, litellm_complete_object: ModelResponse) -> None:
        """
        Ensure tool calls that were not streamed as deltas still get emitted before response.completed.
        """
        if self._final_tool_events_queued:
            return
        self._final_tool_events_queued = True

        try:
            message: Final = litellm_complete_object.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
        except Exception:
            tool_calls = None

        if not tool_calls or not isinstance(tool_calls, list):
            return

        for tc in tool_calls:
            call_id_raw = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not call_id_raw:
                continue
            call_id = str(call_id_raw)
            output_index = self._get_or_assign_tool_output_index(call_id)

            fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
            fn_name = ""
            fn_args = ""
            if isinstance(fn, dict):
                fn_name = str(fn.get("name") or "")
                fn_args = str(fn.get("arguments") or "")
            else:
                fn_name = str(getattr(fn, "name", "") or "")
                fn_args = str(getattr(fn, "arguments", "") or "")
            tool_name, tool_namespace, namespace_tool_type = self._resolve_tool_call_fields(call_id, fn_name)
            custom_tool_names = self._custom_names_for_tool_call(
                tool_name,
                tool_namespace,
                namespace_tool_type,
            )

            # Track if this is a new tool call that wasn't streamed
            is_new_tool_call = call_id not in self._tool_args_by_call_id

            # If we never sent output_item.added for this call_id, emit it now.
            if is_new_tool_call:
                self._tool_args_by_call_id[call_id] = ""
                self._sequence_number += 1
                item_kwargs = build_tool_call_item_kwargs(
                    call_id,
                    tool_name,
                    "",
                    "in_progress",
                    custom_tool_names,
                )
                if tool_namespace:
                    item_kwargs["namespace"] = tool_namespace
                event = OutputItemAddedEvent(
                    type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
                    output_index=output_index,
                    item=BaseLiteLLMOpenAIResponseObject(**item_kwargs),
                    sequence_number=self._sequence_number,
                )
                self._pending_tool_events.append(event)

            final_args = fn_args or self._tool_args_by_call_id.get(call_id, "")
            is_custom_tool = tool_name in custom_tool_names

            # Emit delta events for arguments that weren't streamed yet
            # This handles cases where Bedrock sends the complete tool call at the end
            already_streamed = self._tool_args_by_call_id.get(call_id, "")
            remaining_args = final_args[len(already_streamed) :] if final_args else ""

            if remaining_args and not is_custom_tool:
                # Split into smaller chunks to match OpenAI's streaming behavior
                chunk_size = 10  # Match typical OpenAI delta size
                for i in range(0, len(remaining_args), chunk_size):
                    delta_chunk = remaining_args[i : i + chunk_size]
                    self._sequence_number += 1
                    delta_event = FunctionCallArgumentsDeltaEvent(
                        type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DELTA,
                        item_id=call_id,
                        output_index=output_index,
                        delta=delta_chunk,
                        sequence_number=self._sequence_number,
                    )
                    self._pending_tool_events.append(delta_event)

            self._sequence_number += 1
            if is_custom_tool:
                custom_input = unwrap_custom_tool_arguments(final_args)
                if custom_input:
                    custom_delta_event = CustomToolCallInputDeltaEvent(
                        type=ResponsesAPIStreamEvents.CUSTOM_TOOL_CALL_INPUT_DELTA,
                        item_id=call_id,
                        output_index=output_index,
                        delta=custom_input,
                        sequence_number=self._sequence_number,
                    )
                    self._pending_tool_events.append(custom_delta_event)
                    self._sequence_number += 1
                done_event: BaseLiteLLMOpenAIResponseObject = CustomToolCallInputDoneEvent(
                    type=ResponsesAPIStreamEvents.CUSTOM_TOOL_CALL_INPUT_DONE,
                    item_id=call_id,
                    output_index=output_index,
                    input=custom_input,
                    sequence_number=self._sequence_number,
                )
            else:
                done_event = FunctionCallArgumentsDoneEvent(
                    type=ResponsesAPIStreamEvents.FUNCTION_CALL_ARGUMENTS_DONE,
                    item_id=call_id,
                    output_index=output_index,
                    arguments=final_args,
                    sequence_number=self._sequence_number,
                )
            self._pending_tool_events.append(done_event)

            self._sequence_number += 1
            item_kwargs = build_tool_call_item_kwargs(
                call_id,
                tool_name,
                final_args,
                "completed",
                custom_tool_names,
            )
            if tool_namespace:
                item_kwargs["namespace"] = tool_namespace
            provider_fields = (
                tc.get("provider_specific_fields")
                if isinstance(tc, dict)
                else getattr(tc, "provider_specific_fields", None)
            )
            if isinstance(provider_fields, dict) and provider_fields:
                item_kwargs["provider_specific_fields"] = provider_fields
            item_done_event = OutputItemDoneEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                output_index=output_index,
                sequence_number=self._sequence_number,
                item=BaseLiteLLMOpenAIResponseObject(**item_kwargs),
            )
            self._completed_tool_steps += ((output_index, item_done_event.item),)
            self._pending_tool_events.append(item_done_event)

    def _default_response_created_event_data(self) -> dict:
        # Use cached response ID if available, otherwise generate a new one
        if self._cached_response_id is None:
            self._cached_response_id = f"resp_{uuid.uuid4()}"

        response_created_event_data: Final = {
            "id": self._cached_response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "instructions": self.responses_api_request.get("instructions", None),
            "max_output_tokens": None,
            "model": self.model,
            "output": [],
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": True,
        }
        if "temperature" in self.responses_api_request:
            response_created_event_data["temperature"] = self.responses_api_request["temperature"]
        if "text" in self.responses_api_request:
            response_created_event_data["text"] = self.responses_api_request["text"]
        if "tool_choice" in self.responses_api_request:
            # Transform tool_choice from dict format (e.g., {"type": "auto"}) to string format
            response_created_event_data["tool_choice"] = (
                LiteLLMCompletionResponsesConfig._transform_tool_choice(self.responses_api_request["tool_choice"])
                or "auto"
            )
        else:
            response_created_event_data["tool_choice"] = "auto"
        if "tools" in self.responses_api_request:
            response_created_event_data["tools"] = self.responses_api_request["tools"]
        else:
            response_created_event_data["tools"] = []
        if "top_p" in self.responses_api_request:
            response_created_event_data["top_p"] = self.responses_api_request["top_p"]
        else:
            response_created_event_data["top_p"] = 1.0
        if "truncation" in self.responses_api_request:
            response_created_event_data["truncation"] = self.responses_api_request["truncation"]
        if "user" in self.responses_api_request:
            response_created_event_data["user"] = self.responses_api_request["user"]
        if "metadata" in self.responses_api_request:
            response_created_event_data["metadata"] = self.responses_api_request["metadata"]
        return response_created_event_data

    def create_response_created_event(self) -> ResponseCreatedEvent:
        """
        data: {"type":"response.created","response":{"id":"resp_67c9fdcecf488190bdd9a0409de3a1ec07b8b0ad4e5eb654","object":"response","created_at":1741290958,"status":"in_progress","error":null,"incomplete_details":null,"instructions":"You are a helpful assistant.","max_output_tokens":null,"model":"gpt-4.1-2025-04-14","output":[],"parallel_tool_calls":true,"previous_response_id":null,"reasoning":{"effort":null,"summary":null},"store":true,"temperature":1.0,"text":{"format":{"type":"text"}},"tool_choice":"auto","tools":[],"top_p":1.0,"truncation":"disabled","usage":null,"user":null,"metadata":{}}}

        """
        response_created_event_data: Final = self._default_response_created_event_data()
        self._sequence_number += 1
        event: Final = ResponseCreatedEvent(
            type=ResponsesAPIStreamEvents.RESPONSE_CREATED,
            response=ResponsesAPIResponse(**response_created_event_data),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        return event

    def create_response_in_progress_event(self) -> ResponseInProgressEvent:
        response_in_progress_event_data: Final = self._default_response_created_event_data()
        response_in_progress_event_data["status"] = "in_progress"
        self._sequence_number += 1
        event: Final = ResponseInProgressEvent(
            type=ResponsesAPIStreamEvents.RESPONSE_IN_PROGRESS,
            response=ResponsesAPIResponse(**response_in_progress_event_data),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        return event

    def create_output_item_added_event(self) -> OutputItemAddedEvent:
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"

        self._sequence_number += 1
        event: Final = OutputItemAddedEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
            output_index=self._current_step_output_index,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": self._cached_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
            ),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        return event

    def create_content_part_added_event(self) -> ContentPartAddedEvent:
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"

        self._sequence_number += 1
        event: Final = ContentPartAddedEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_ADDED,
            item_id=self._cached_item_id,
            output_index=self._current_step_output_index,
            content_index=0,
            part=BaseLiteLLMOpenAIResponseObject(**{"type": "output_text", "text": "", "annotations": []}),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        return event

    def _merge_provider_specific_fields(self, src: dict) -> None:
        """Merge provider_specific_fields using last-value-wins for lists.

        List-valued keys (web_search_results, tool_results,
        code_interpreter_results, etc.) are emitted cumulatively — each
        emission contains the full list so far.  Using "last value wins"
        matches stream_chunk_builder's semantics and avoids quadratic
        growth from repeated extend calls.
        """
        for key, val in src.items():
            self._accumulated_provider_specific_fields[key] = val

    def create_litellm_model_response(self) -> ModelResponse | None:
        response: Final = cast(
            ModelResponse | None,
            stream_chunk_builder(
                chunks=self.collected_chat_completion_chunks,
                logging_obj=self.litellm_logging_obj,
            ),
        )
        if response is not None and self._accumulated_provider_specific_fields:
            if not hasattr(response, "_hidden_params") or response._hidden_params is None:
                response._hidden_params = {}
            response._hidden_params.setdefault("provider_specific_fields", {}).update(
                self._accumulated_provider_specific_fields
            )
        if response is not None and self._completed_step_tool_calls and response.choices:
            first_choice: Final = response.choices[0]
            return response.model_copy(
                update={
                    "choices": [
                        first_choice.model_copy(
                            update={
                                "message": first_choice.message.model_copy(
                                    update={"tool_calls": list(self._completed_step_tool_calls)}
                                )
                            }
                        ),
                        *response.choices[1:],
                    ]
                }
            )
        return response

    @staticmethod
    def _snapshot_chunk_for_stream_chunk_builder(
        chunk: ModelResponseStream,
    ) -> dict[str, Any]:
        """
        Convert a streaming chunk into a plain dict for end-of-stream assembly.
        Keep _hidden_params so downstream usage/header behavior is preserved.
        """
        chunk_dict: Final = chunk.model_dump()
        hidden_params: Final = getattr(chunk, "_hidden_params", None)
        if hidden_params is not None:
            chunk_dict["_hidden_params"] = dict(hidden_params) if isinstance(hidden_params, dict) else hidden_params
        return chunk_dict

    def create_reasoning_summary_text_done_event(
        self,
        reasoning_item_id: str,
        reasoning_content: str,
        sequence_number: int,
    ) -> ReasoningSummaryTextDoneEvent:
        """
        Create response.reasoning_summary_text.done event.

        Example:
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": "rs_0c5dae30e53172980069708ba2f59c8197b71ca9820edad07c",
            "output_index": 0,
            "sequence_number": 97,
            "summary_index": 0,
            "text": "**Clarifying the first humans**\n\nThe  I'm addressing the user's specific interest."
        }
        """
        return ReasoningSummaryTextDoneEvent(
            type=ResponsesAPIStreamEvents.REASONING_SUMMARY_TEXT_DONE,
            item_id=reasoning_item_id,
            output_index=self._current_reasoning_output_index,
            sequence_number=sequence_number,
            summary_index=0,
            text=reasoning_content,
        )

    def create_reasoning_summary_part_done_event(
        self,
        reasoning_item_id: str,
        reasoning_content: str,
        sequence_number: int,
    ) -> ReasoningSummaryPartDoneEvent:
        """
        Create response.reasoning_summary_part.done event.

        Example:
        {
            "type": "response.reasoning_summary_part.done",
            "item_id": "rs_0c5dae30e53172980069708ba2f59c8197b71ca9820edad07c",
            "output_index": 0,
            "part": {
                "type": "summary_text",
                "text": "**Clarifying the first humans**\n\nThe  earlier hominins. It feels important to ensure I'm addressing the user's specific interest."
            },
            "sequence_number": 98,
            "summary_index": 0
        }
        """
        return ReasoningSummaryPartDoneEvent(
            type=ResponsesAPIStreamEvents.REASONING_SUMMARY_PART_DONE,
            item_id=reasoning_item_id,
            output_index=self._current_reasoning_output_index,
            sequence_number=sequence_number,
            summary_index=0,
            part=BaseLiteLLMOpenAIResponseObject(
                **{
                    "type": "summary_text",
                    "text": reasoning_content,
                }
            ),
        )

    def create_output_text_done_event(self, litellm_complete_object: ModelResponse) -> OutputTextDoneEvent:
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"

        return OutputTextDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            item_id=self._cached_item_id,
            output_index=self._current_step_output_index,
            content_index=0,
            text=getattr(litellm_complete_object.choices[0].message, "content", "") or "",
        )

    def create_output_content_part_done_event(self, litellm_complete_object: ModelResponse) -> ContentPartDoneEvent:
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"

        text: Final = getattr(litellm_complete_object.choices[0].message, "content", "") or ""
        reasoning_content = getattr(litellm_complete_object.choices[0].message, "reasoning_content", "") or ""
        annotations: Final = getattr(litellm_complete_object.choices[0].message, "annotations", None)

        part: PART_UNION_TYPES | None = None
        if reasoning_content:
            part = ContentPartDonePartReasoningText(
                type="reasoning_text",
                reasoning=reasoning_content,
            )

        else:
            response_annotations: Final = (
                LiteLLMCompletionResponsesConfig._transform_chat_completion_annotations_to_response_output_annotations(
                    annotations=annotations
                )
            )
            part = ContentPartDonePartOutputText(
                type="output_text",
                text=text,
                annotations=response_annotations,
                logprobs=None,
            )

        return ContentPartDoneEvent(
            type=ResponsesAPIStreamEvents.CONTENT_PART_DONE,
            item_id=self._cached_item_id,
            output_index=self._current_step_output_index,
            content_index=0,
            part=part,
        )

    def create_output_item_done_event(self, litellm_complete_object: ModelResponse) -> OutputItemDoneEvent:
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"

        text: Final = self.litellm_model_response.choices[0].message.content or ""
        annotations = getattr(self.litellm_model_response.choices[0].message, "annotations", None)

        response_annotations: Final = (
            LiteLLMCompletionResponsesConfig._transform_chat_completion_annotations_to_response_output_annotations(
                annotations=annotations
            )
        )
        return OutputItemDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
            output_index=self._current_step_output_index,
            sequence_number=1,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": self._cached_item_id,
                    "status": "completed",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": response_annotations,
                        }
                    ],
                }
            ),
        )

    def create_reasoning_output_item_done_event(
        self,
        reasoning_item_id: str,
        reasoning_content: str,
        sequence_number: int,
    ) -> OutputItemDoneEvent:
        """
        Create response.output_item.done event for reasoning items.

        Example:
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "sequence_number": 99,
            "item": {
                "id": "rs_0c5dae30e53172980069708ba2f59c8197b71ca9820edad07c",
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": "**Clarifying the first humans**..."
                    }
                ]
            }
        }
        """
        return OutputItemDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
            output_index=self._current_reasoning_output_index,
            sequence_number=sequence_number,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": reasoning_item_id,
                    "type": "reasoning",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": reasoning_content,
                        }
                    ],
                }
            ),
        )

    def return_default_done_events(
        self, litellm_complete_object: ModelResponse
    ) -> BaseLiteLLMOpenAIResponseObject | None:
        if self.sent_output_text_done_event is False:
            self.sent_output_text_done_event = True
            return self.create_output_text_done_event(litellm_complete_object)
        if self.sent_output_content_part_done_event is False:
            self.sent_output_content_part_done_event = True
            return self.create_output_content_part_done_event(litellm_complete_object)
        if self.sent_output_item_done_event is False:
            self.sent_output_item_done_event = True
            return self.create_output_item_done_event(litellm_complete_object)
        return None

    def return_default_initial_events(
        self,
    ) -> BaseLiteLLMOpenAIResponseObject | None:
        if self.sent_response_created_event is False:
            self.sent_response_created_event = True
            return self.create_response_created_event()
        elif self.sent_response_in_progress_event is False:
            self.sent_response_in_progress_event = True
            return self.create_response_in_progress_event()
        return None

    def is_stream_finished(self) -> bool:
        if (
            self.sent_output_text_done_event is True
            and self.sent_output_content_part_done_event is True
            and self.sent_output_item_done_event is True
        ):
            return True
        return False

    def common_done_event_logic(self, sync_mode: bool = True) -> BaseLiteLLMOpenAIResponseObject:
        self._finalize_output_step()
        if not self.litellm_model_response or isinstance(self.litellm_model_response, TextCompletionResponse):
            self.litellm_model_response = self.create_litellm_model_response()
        if self.litellm_model_response:
            # If tool calls exist, emit tool events before finishing/response.completed.
            if isinstance(self.litellm_model_response, ModelResponse):
                self._queue_final_tool_call_done_events(self.litellm_model_response)
            if self._pending_tool_events:
                return self._pending_tool_events.pop(0)

            if (
                self._cached_item_id
                and self.sent_content_part_added_event
                and self.sent_output_text_done_event is False
            ):
                self._queue_message_step_done_events(
                    self._cached_item_id,
                    self._current_step_text,
                    self._current_step_annotations,
                )
                self.sent_output_text_done_event = True
                self.sent_output_content_part_done_event = True
                self.sent_output_item_done_event = True
            if self._pending_response_events:
                return self._pending_response_events.pop(0)

            if not self.sent_content_part_added_event and (
                self._tool_args_by_call_id or self._completed_reasoning_steps
            ):
                self.sent_output_text_done_event = True
                self.sent_output_content_part_done_event = True
                self.sent_output_item_done_event = True

            done_event: Final = self.return_default_done_events(self.litellm_model_response)
            if done_event:
                return done_event
        else:
            if sync_mode:
                raise StopIteration
            else:
                raise StopAsyncIteration

        self.finished = self.is_stream_finished()
        response_completed_event: Final = self._emit_response_completed_event(self.litellm_model_response)
        if response_completed_event:
            # Latch so wrappers (FallbackResponsesStreamWrapper) + proxy
            # container-ownership hook can read completed_response.
            self.completed_response = response_completed_event
            return response_completed_event
        else:
            if sync_mode:
                raise StopIteration
            else:
                raise StopAsyncIteration

    def _ensure_output_item_for_chunk(self, chunk: ModelResponseStream) -> None:
        if not chunk.choices:
            return
        delta: Final = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None) or getattr(delta, "thinking_blocks", None):
            if self._reasoning_active:
                return
            if self._reasoning_done_emitted:
                self._cached_reasoning_item_id = None
                self._accumulated_reasoning_content_parts = []
            self._reasoning_active = True
            self._reasoning_done_emitted = False
            self._current_reasoning_output_index = self._allocate_output_index()
            if self._cached_reasoning_item_id is None:
                self._cached_reasoning_item_id = f"rs_{uuid.uuid4()}"
            self._reasoning_item_id = self._cached_reasoning_item_id
            self._sequence_number += 1
            reasoning_event: Final = OutputItemAddedEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
                output_index=self._current_reasoning_output_index,
                item=BaseLiteLLMOpenAIResponseObject(
                    **{
                        "id": self._cached_reasoning_item_id,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    }
                ),
                sequence_number=self._sequence_number,
            )
            self._pending_response_events.append(reasoning_event)
            return
        if hasattr(delta, "tool_calls") and delta.tool_calls:
            return
        if self.sent_content_part_added_event:
            return
        self._current_step_output_index = self._allocate_output_index()
        self._sequence_number += 1
        self.sent_output_item_added_event = True
        self._cached_item_id = self._cached_item_id or f"msg_{uuid.uuid4()}"
        event: Final = OutputItemAddedEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_ITEM_ADDED,
            output_index=self._current_step_output_index,
            item=BaseLiteLLMOpenAIResponseObject(
                **{
                    "id": self._cached_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
            ),
        )
        event.__dict__["sequence_number"] = self._sequence_number
        self._pending_response_events.append(event)

        # Emit content_part.added immediately after output_item.added for message
        # items. The OpenAI Responses spec requires this event before any
        # output_text.delta events so downstream parsers can initialize the
        # text part structure.
        if not self.sent_content_part_added_event:
            self.sent_content_part_added_event = True
            content_part_event: Final = self.create_content_part_added_event()
            self._pending_response_events.append(content_part_event)
        return

    def _prepare_output_step_for_chunk(self, chunk: ModelResponseStream) -> None:
        if not chunk.choices:
            return
        chunk_id = chunk.id
        delta_role = chunk.choices[0].delta.role if chunk.choices else None
        starts_new_step = bool(
            chunk_id
            and self._active_upstream_chunk_id
            and chunk_id != self._active_upstream_chunk_id
            and (self._upstream_step_finished or delta_role == "assistant")
        )
        if starts_new_step:
            self._finalize_output_step()
            self.sent_output_item_added_event = False
            self.sent_content_part_added_event = False
            self.sent_output_text_done_event = False
            self.sent_output_content_part_done_event = False
            self.sent_output_item_done_event = False
            self.sent_annotation_events = False
            self._cached_item_id = None
            self._current_step_text = ""
            self._current_step_finish_reason = None
            self._current_step_annotations = []
            self._cached_reasoning_item_id = None
            self._reasoning_item_id = None
            self._reasoning_done_emitted = False
            self._reasoning_active = False
            self._accumulated_reasoning_content_parts = []
            self._tool_call_id_by_index = {}
            self._ambiguous_tool_call_indexes = set()
            self._final_tool_events_queued = False
            self._current_step_chunk_start = len(self.collected_chat_completion_chunks)
            self._current_step_finalized = False
            if hasattr(self, "_pending_annotation_events"):
                del self._pending_annotation_events
            self._active_upstream_chunk_id = chunk_id
            self._upstream_step_finished = False
        elif self._active_upstream_chunk_id is None and chunk_id:
            self._active_upstream_chunk_id = chunk_id

        if chunk.choices and chunk.choices[0].finish_reason is not None:
            self._upstream_step_finished = True
            self._current_step_finish_reason = chunk.choices[0].finish_reason

    def _current_step_response(self) -> ModelResponse | None:
        chunks: Final = self.collected_chat_completion_chunks[self._current_step_chunk_start :]
        if not chunks:
            return None
        response: Final = stream_chunk_builder(chunks=chunks, logging_obj=self.litellm_logging_obj)
        return response if isinstance(response, ModelResponse) else None

    def _queue_reasoning_done_events(self, response: ModelResponse | None = None) -> None:
        if not self._reasoning_active or self._reasoning_done_emitted or self._cached_reasoning_item_id is None:
            return
        reasoning_content: Final = "".join(self._accumulated_reasoning_content_parts)
        self._sequence_number += 1
        text_event: Final = self.create_reasoning_summary_text_done_event(
            self._cached_reasoning_item_id, reasoning_content, self._sequence_number
        )
        self._sequence_number += 1
        part_event: Final = self.create_reasoning_summary_part_done_event(
            self._cached_reasoning_item_id, reasoning_content, self._sequence_number
        )
        self._sequence_number += 1
        item_event: Final = self.create_reasoning_output_item_done_event(
            self._cached_reasoning_item_id, reasoning_content, self._sequence_number
        )
        step_response: Final = response or self._current_step_response()
        encrypted_content: Final = (
            LiteLLMCompletionResponsesConfig._encode_thinking_blocks(step_response.choices[0].message)
            if step_response is not None and step_response.choices
            else None
        )
        if encrypted_content is not None:
            setattr(item_event.item, "encrypted_content", encrypted_content)
        self._completed_reasoning_steps += ((self._current_reasoning_output_index, item_event.item),)
        self._pending_response_events.extend((text_event, part_event, item_event))
        self._reasoning_done_emitted = True
        self._reasoning_active = False

    def _finalize_output_step(self) -> None:
        if self._current_step_finalized:
            return
        response: Final = self._current_step_response()
        if response is None:
            return
        self._current_step_finalized = True
        self._queue_reasoning_done_events(response)
        self._queue_final_tool_call_done_events(response)
        self._pending_response_events.extend(self._pending_tool_events)
        self._pending_tool_events = []
        if response.choices:
            self._completed_step_tool_calls += tuple(response.choices[0].message.tool_calls or ())
        if self._cached_item_id and self.sent_content_part_added_event and not self.sent_output_item_done_event:
            self._queue_message_step_done_events(
                self._cached_item_id, self._current_step_text, self._current_step_annotations
            )
            self.sent_output_text_done_event = True
            self.sent_output_content_part_done_event = True
            self.sent_output_item_done_event = True

    def _queue_message_step_done_events(
        self,
        item_id: str,
        text: str,
        annotations: list[BaseLiteLLMOpenAIResponseObject] | None = None,
    ) -> None:
        response_annotations: Final = annotations if annotations is not None else []
        message_item: Final = BaseLiteLLMOpenAIResponseObject.model_validate(
            {
                "id": item_id,
                "status": LiteLLMCompletionResponsesConfig._map_chat_completion_finish_reason_to_responses_status(
                    self._current_step_finish_reason
                ),
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": response_annotations}],
            }
        )
        self._completed_message_steps += ((self._current_step_output_index, message_item),)
        self._sequence_number += 1
        self._pending_response_events.append(
            OutputTextDoneEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
                item_id=item_id,
                output_index=self._current_step_output_index,
                content_index=0,
                text=text,
            ).model_copy(update={"sequence_number": self._sequence_number})
        )
        self._sequence_number += 1
        self._pending_response_events.append(
            ContentPartDoneEvent(
                type=ResponsesAPIStreamEvents.CONTENT_PART_DONE,
                item_id=item_id,
                output_index=self._current_step_output_index,
                content_index=0,
                part=ContentPartDonePartOutputText(
                    type="output_text",
                    text=text,
                    annotations=response_annotations,
                    logprobs=None,
                ),
            ).model_copy(update={"sequence_number": self._sequence_number})
        )
        self._sequence_number += 1
        self._pending_response_events.append(
            OutputItemDoneEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                output_index=self._current_step_output_index,
                sequence_number=self._sequence_number,
                item=message_item,
            )
        )

    def _process_chat_completion_chunk(self, chunk: ModelResponseStream) -> None:
        if not self.collected_chat_completion_chunks:
            self._next_tool_output_index = 0
        self._prepare_output_step_for_chunk(chunk)
        for source in (
            getattr(chunk, "provider_specific_fields", None),
            getattr(chunk.choices[0].delta, "provider_specific_fields", None) if chunk.choices else None,
        ):
            if isinstance(source, dict) and source:
                self._merge_provider_specific_fields(source)
        self.collected_chat_completion_chunks.append(self._snapshot_chunk_for_stream_chunk_builder(chunk))
        if not chunk.choices:
            return
        choice: Final = chunk.choices[0]
        delta: Final = choice.delta
        thinking_blocks: Final = getattr(delta, "thinking_blocks", None) or ()
        reasoning: Final = getattr(delta, "reasoning_content", None) or "".join(
            block.get("thinking") or "" for block in thinking_blocks if block.get("type") == "thinking"
        )
        if reasoning or thinking_blocks:
            reasoning_chunk: Final = chunk.model_copy(
                update={
                    "choices": [
                        choice.model_copy(
                            update={"delta": delta.model_copy(update={"content": None, "tool_calls": None})}
                        )
                    ]
                }
            )
            self._ensure_output_item_for_chunk(reasoning_chunk)
            self._accumulated_reasoning_content_parts.append(reasoning)
            if reasoning and self._cached_reasoning_item_id is not None:
                self._sequence_number += 1
                self._pending_response_events.append(
                    ReasoningSummaryTextDeltaEvent(
                        type=ResponsesAPIStreamEvents.REASONING_SUMMARY_TEXT_DELTA,
                        item_id=self._cached_reasoning_item_id,
                        output_index=self._current_reasoning_output_index,
                        delta=reasoning,
                    ).model_copy(update={"sequence_number": self._sequence_number})
                )
        if delta.content or delta.tool_calls or choice.finish_reason is not None:
            self._queue_reasoning_done_events()
        if (
            delta.content
            or getattr(delta, "annotations", None)
            or (
                delta.role
                and not reasoning
                and not thinking_blocks
                and not delta.tool_calls
                and not self.sent_output_item_added_event
                and not self._completed_reasoning_steps
            )
        ):
            text_chunk: Final = chunk.model_copy(
                update={
                    "choices": [
                        choice.model_copy(
                            update={
                                "delta": delta.model_copy(
                                    update={"reasoning_content": None, "thinking_blocks": None, "tool_calls": None}
                                )
                            }
                        )
                    ]
                }
            )
            self._ensure_output_item_for_chunk(text_chunk)
            text_event: Final = self._transform_chat_completion_chunk_to_response_api_chunk(text_chunk)
            if text_event is not None:
                self._pending_response_events.append(text_event)
            for annotation_event in getattr(self, "_pending_annotation_events", ()):
                self._sequence_number += 1
                annotation_event.sequence_number = self._sequence_number
                self._pending_response_events.append(annotation_event)
            self._pending_annotation_events = []
        if delta.tool_calls:
            self._queue_tool_call_delta_events(delta.tool_calls)
            self._pending_response_events.extend(self._pending_tool_events)
            self._pending_tool_events = []

    async def __anext__(
        self,
    ) -> ResponsesAPIStreamingResponse | ResponseCompletedEvent | BaseLiteLLMOpenAIResponseObject:
        try:
            while True:
                if self.finished is True:
                    raise StopAsyncIteration

                result = self.return_default_initial_events()
                if result:
                    return result
                # Emit any pending output_item or other response events before reading a new chunk
                if self._pending_response_events:
                    return self._pending_response_events.pop(0)
                # Emit any pending tool events before reading a new chunk
                if self._pending_tool_events:
                    return self._pending_tool_events.pop(0)
                if hasattr(self, "_pending_annotation_events") and self._pending_annotation_events:
                    return self._pending_annotation_events.pop(0)

                try:
                    chunk = await self.litellm_custom_stream_wrapper.__anext__()
                    if chunk is not None:
                        chunk = cast(ModelResponseStream, chunk)
                        self._process_chat_completion_chunk(chunk)

                    if self._pending_response_events:
                        return self._pending_response_events.pop(0)

                except StopAsyncIteration:
                    return self.common_done_event_logic(sync_mode=False)

        except Exception as e:
            # Handle HTTP errors
            self.finished = True
            raise e

    def __iter__(self):
        return self

    def __next__(
        self,
    ) -> ResponsesAPIStreamingResponse | ResponseCompletedEvent | BaseLiteLLMOpenAIResponseObject:
        try:
            while True:
                if self.finished is True:
                    raise StopIteration
                result = self.return_default_initial_events()
                if result:
                    return result
                # Emit any pending output_item or other response events before reading a new chunk
                if self._pending_response_events:
                    return self._pending_response_events.pop(0)
                # Emit any pending tool events before reading a new chunk
                if self._pending_tool_events:
                    return self._pending_tool_events.pop(0)
                if hasattr(self, "_pending_annotation_events") and self._pending_annotation_events:
                    return self._pending_annotation_events.pop(0)
                try:
                    chunk = self.litellm_custom_stream_wrapper.__next__()
                    if chunk is not None:
                        self._process_chat_completion_chunk(cast(ModelResponseStream, chunk))
                    if self._pending_response_events:
                        return self._pending_response_events.pop(0)
                    # Otherwise, loop to next chunk
                except StopIteration:
                    return self.common_done_event_logic(sync_mode=True)
        except Exception as e:
            # Handle HTTP errors
            self.finished = True
            raise e

    def _transform_chat_completion_chunk_to_response_api_chunk(
        self, chunk: ModelResponseStream
    ) -> ResponsesAPIStreamingResponse | None:
        """
        Transform a chat completion chunk to a response API chunk.

        This currently handles emitting the OutputTextDeltaEvent, which is used by other tools using the responses API
        and the ReasoningSummaryTextDeltaEvent, which is used by the responses API to emit reasoning content.
        It also handles emitting annotation.added events when annotations are detected in the chunk.
        """
        if not chunk.choices:
            return None
        if self._cached_item_id is None:
            self._cached_item_id = f"msg_{uuid.uuid4()}"
        item_id: Final = self._cached_item_id

        # Check if this chunk has annotations first (before processing text/reasoning)
        # This ensures we detect and queue annotation events from the annotation chunk
        if chunk.choices and hasattr(chunk.choices[0].delta, "annotations"):
            annotations: Final = chunk.choices[0].delta.annotations
            if annotations:
                self.sent_annotation_events = True
                response_annotations = LiteLLMCompletionResponsesConfig._transform_chat_completion_annotations_to_response_output_annotations(
                    annotations=annotations
                )
                annotation_start_index = len(self._current_step_annotations)
                self._current_step_annotations.extend(response_annotations)
                pending_annotation_events = getattr(self, "_pending_annotation_events", [])
                self._pending_annotation_events = [
                    *pending_annotation_events,
                    *[
                        OutputTextAnnotationAddedEvent(
                            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_ANNOTATION_ADDED,
                            item_id=item_id,
                            output_index=self._current_step_output_index,
                            content_index=0,
                            annotation_index=annotation_start_index + idx,
                            annotation=(
                                annotation.model_dump() if hasattr(annotation, "model_dump") else dict(annotation)
                            ),
                        )
                        for idx, annotation in enumerate(response_annotations)
                    ],
                ]
        # Priority 1: Handle reasoning content (highest priority)
        if (
            chunk.choices
            and hasattr(chunk.choices[0].delta, "reasoning_content")
            and chunk.choices[0].delta.reasoning_content
        ):
            reasoning_content: Final = chunk.choices[0].delta.reasoning_content

            if self._cached_reasoning_item_id is None:
                self._cached_reasoning_item_id = f"rs_{uuid.uuid4()}"

            return ReasoningSummaryTextDeltaEvent(
                type=ResponsesAPIStreamEvents.REASONING_SUMMARY_TEXT_DELTA,
                item_id=self._cached_reasoning_item_id,
                output_index=self._current_step_output_index,
                delta=reasoning_content,
            )

        # Priority 2: Handle text deltas
        delta_content: Final = self._get_delta_string_from_streaming_choices(chunk.choices)
        if delta_content:
            self._current_step_text += delta_content
            self._sequence_number += 1
            text_delta_event: Final = OutputTextDeltaEvent(
                type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
                item_id=item_id,
                output_index=self._current_step_output_index,
                content_index=0,
                delta=delta_content,
            )
            text_delta_event.__dict__["sequence_number"] = self._sequence_number
            return text_delta_event

        # Priority 3: Handle tool call deltas (if any) -> queue events and emit them
        # For each tool call delta, we emit events one at a time to match OpenAI's streaming behavior
        if chunk.choices and hasattr(chunk.choices[0].delta, "tool_calls") and chunk.choices[0].delta.tool_calls:
            self._queue_tool_call_delta_events(chunk.choices[0].delta.tool_calls)
            # Return one pending tool event at a time
            if self._pending_tool_events:
                return self._pending_tool_events.pop(0)

        # Priority 4: If we have pending annotation events, emit the next one
        # This happens when the current chunk has no text/reasoning content
        if hasattr(self, "_pending_annotation_events") and self._pending_annotation_events:
            event = self._pending_annotation_events.pop(0)
            return event

        # Priority 5: If we have pending tool events (from earlier chunk), emit the next one
        if self._pending_tool_events:
            return self._pending_tool_events.pop(0)

        return None

    def _get_delta_string_from_streaming_choices(self, choices: list[StreamingChoices]) -> str:
        """
        Get the delta string from the streaming choices

        For now this collected the first choice's delta string.

        It's unclear how users expect litellm to translate multiple-choices-per-chunk to the responses API output.
        """
        if not choices:
            return ""
        choice: Final = choices[0]
        chat_completion_delta: Final[ChatCompletionDelta] = choice.delta
        return chat_completion_delta.content or ""

    def _output_with_streamed_item_ids(self, responses_api_response: ResponsesAPIResponse) -> tuple[Any, ...]:
        """
        Reuse the item IDs already emitted by the incremental streaming events in the
        ``response.completed`` snapshot, so a streaming client that replays the snapshot
        sends back the same IDs it observed mid-stream.
        """
        completed_items: Final = (
            self._completed_message_steps + self._completed_reasoning_steps + self._completed_tool_steps
        )
        if completed_items:
            completed_ids: Final = frozenset(getattr(item, "id", None) for _, item in completed_items)
            replaced_types: Final = frozenset({"message", "reasoning", "function_call", "custom_tool_call"})
            remaining_items: Final = tuple(
                item
                for item in responses_api_response.output or ()
                if getattr(item, "type", None) not in replaced_types and getattr(item, "id", None) not in completed_ids
            )
            replacements: Final = {
                getattr(item, "id", None): item
                for item in responses_api_response.output or ()
                if getattr(item, "type", None) not in replaced_types
            }
            return (
                *(
                    replacements.get(getattr(item, "id", None), item)
                    for _, item in sorted(completed_items, key=lambda pair: pair[0])
                ),
                *remaining_items,
            )

        message_aligned: Final = _output_items_with_id(
            tuple(responses_api_response.output or ()),
            "message",
            self._cached_item_id,
        )
        return _output_items_with_id(message_aligned, "reasoning", self._cached_reasoning_item_id)

    def _emit_response_completed_event(self, litellm_model_response: ModelResponse) -> ResponseCompletedEvent | None:
        if litellm_model_response:
            # Add cost to usage object if include_cost_in_streaming_usage is True
            if litellm.include_cost_in_streaming_usage and self.litellm_logging_obj is not None:
                usage: Final = getattr(litellm_model_response, "usage", None)
                if usage is not None:
                    setattr(
                        usage,
                        "cost",
                        self.litellm_logging_obj._response_cost_calculator(result=litellm_model_response),
                    )

            response_for_transformation = (
                litellm_model_response.model_copy(update={"id": self._cached_response_id})
                if self._cached_response_id
                else litellm_model_response
            )

            # Transform the response
            responses_api_response: Final = (
                LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
                    request_input=self.request_input,
                    chat_completion_response=response_for_transformation,
                    responses_api_request=self.responses_api_request,
                )
            )

            # Use the cached response ID to ensure consistency across all events
            if self._cached_response_id:
                responses_api_response.id = self._cached_response_id

            responses_api_response.output = list(self._output_with_streamed_item_ids(responses_api_response))

            # Encode the response ID to match non-streaming behavior
            encoded_response: Final = ResponsesAPIRequestUtils._update_responses_api_response_id_with_model_id(
                responses_api_response=responses_api_response,
                custom_llm_provider=self.custom_llm_provider,
                litellm_metadata=self.litellm_metadata,
            )

            self._sequence_number += 1
            return ResponseCompletedEvent(
                type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
                response=encoded_response,
            ).model_copy(update={"sequence_number": self._sequence_number})
        else:
            return None
