"""Responses bridge for a Router-authorized DeepSeek Anthropic deployment."""

import json
import time
from copy import deepcopy
from datetime import datetime
from inspect import isawaitable
from typing import Mapping
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from litellm.constants import (
    DEFAULT_REASONING_EFFORT_HIGH_THINKING_BUDGET,
    DEFAULT_REASONING_EFFORT_LOW_THINKING_BUDGET,
    DEFAULT_REASONING_EFFORT_MAX_THINKING_BUDGET,
)
from litellm.litellm_core_utils.asyncify import run_async_function
from litellm.anthropic_beta_headers_manager import update_headers_with_filtered_beta
from litellm.litellm_core_utils.get_provider_specific_headers import ProviderSpecificHeaderUtils
from litellm.llms.deepseek.anthropic_protocol import (
    DeepSeekProtocolNonFallbackError,
    DeepSeekProtocolError,
    DeepSeekUpstreamError,
    compile_deepseek_anthropic_history,
)
from litellm.llms.deepseek.messages.transformation import DeepSeekAnthropicMessagesConfig
from litellm.llms.deepseek.responses_transport import (
    DeepSeekRawFailure,
    DeepSeekResponsesRawTransport,
    freeze_deepseek_request,
)
from litellm.responses.deepseek_accounting import (
    AttemptRateSnapshot,
    DeepSeekParentAccountingTracker,
    ParentAccounting,
    build_attempt_snapshot,
)
from litellm.responses.deepseek_session import (
    SpendLogDeepSeekResponsesSessionRepository,
    create_deepseek_responses_session,
)
from litellm.responses.deepseek_streaming import (
    DeepSeekAnthropicResponsesAsyncStream,
    DeepSeekAnthropicResponsesSyncStream,
)
from litellm.responses.utils import ResponseAPILoggingUtils, ResponsesAPIRequestUtils
from litellm.router_protocol import DeploymentProtocolContext
from litellm.types.llms.openai import (
    ResponseAPIUsage,
    ResponseInputParam,
    ResponsesAPIOptionalRequestParams,
    ResponsesAPIResponse,
)
from litellm.types.router import GenericLiteLLMParams

_PROTOCOL_INTEGRITY_CODES = frozenset(
    {
        "reasoning_history_missing",
        "reasoning_history_unrecoverable",
        "reasoning_history_context_exhausted",
        "reasoning_mode_conflict",
        "tool_history_invalid",
        "tool_result_orphaned",
        "tool_history_incomplete",
        "unsupported_input_item",
        "tool_choice_invalid",
        "reasoning_history_persistence_failed",
        "reasoning_history_persistence_unavailable",
        "router_provenance_required",
        "upstream_response_invalid",
    }
)
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, object])
_DEFAULT_MAX_OUTPUT_TOKENS = 4096
_REASONING_EFFORT_BUDGETS = {
    "low": DEFAULT_REASONING_EFFORT_LOW_THINKING_BUDGET,
    "high": DEFAULT_REASONING_EFFORT_HIGH_THINKING_BUDGET,
    "max": DEFAULT_REASONING_EFFORT_MAX_THINKING_BUDGET,
}


def _new_deepseek_response_id() -> str:
    return f"resp_ds_{uuid4().hex}"


def _error_code_from_upstream_body(body: bytes) -> str | None:
    try:
        parsed = _JSON_OBJECT_ADAPTER.validate_json(body)
    except ValidationError:
        return None
    code = parsed.get("code")
    if isinstance(code, str) and code in _PROTOCOL_INTEGRITY_CODES:
        return code
    nested_error = parsed.get("error")
    if isinstance(nested_error, dict):
        nested_code = _JSON_OBJECT_ADAPTER.validate_python(nested_error).get("code")
        if isinstance(nested_code, str) and nested_code in _PROTOCOL_INTEGRITY_CODES:
            return nested_code
    # Some DeepSeek-compatible gateways return only Anthropic's generic
    # ``type`` and a human-readable message.  Preserve fallback for ordinary
    # invalid requests, but classify messages that prove the request's
    # reasoning/tool history could not be accepted as a protocol failure.
    message_values: list[str] = []

    def _collect_strings(value: object) -> None:
        if isinstance(value, str):
            message_values.append(value.lower())
        elif isinstance(value, Mapping):
            for nested in value.values():
                _collect_strings(nested)
        elif isinstance(value, list):
            for nested in value:
                _collect_strings(nested)

    _collect_strings(parsed)
    message = " ".join(message_values)
    history_markers = (
        "reasoning_content",
        "reasoning history",
        "thinking block",
        "tool_result",
        "tool result",
        "tool_use",
        "tool use",
        "tool call",
        "call_id",
        "previous_response_id",
    )
    if any(marker in message for marker in history_markers):
        return "reasoning_history_unrecoverable"
    return None


def _raw_failure_exception(failure: DeepSeekRawFailure) -> DeepSeekProtocolError | DeepSeekUpstreamError:
    code = _error_code_from_upstream_body(failure.body)
    if code is not None:
        return DeepSeekProtocolError(code, raw_headers=failure.headers, raw_body=failure.body)
    return DeepSeekUpstreamError(
        failure.category,
        failure.status_code,
        raw_headers=failure.headers,
        raw_body=failure.body,
    )


def _resolved_max_output_tokens(request: ResponsesAPIOptionalRequestParams) -> int:
    max_output_tokens = request.get("max_output_tokens")
    if max_output_tokens is None:
        return _DEFAULT_MAX_OUTPUT_TOKENS
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 2:
        raise DeepSeekProtocolError("reasoning_budget_invalid")
    return max_output_tokens


def _effort_to_thinking(reasoning: object, *, max_output_tokens: int) -> tuple[dict[str, object], bool]:
    if reasoning is None:
        effort = "high"
    elif isinstance(reasoning, Mapping):
        effort_value = reasoning.get("effort", "high")
        if not isinstance(effort_value, str):
            raise DeepSeekProtocolError("reasoning_effort_invalid")
        effort = effort_value
    else:
        raise DeepSeekProtocolError("reasoning_effort_invalid")
    if effort not in {"none", "low", "high", "max"}:
        raise DeepSeekProtocolError("reasoning_effort_invalid")
    if effort == "none":
        return {"type": "disabled"}, False
    # DeepSeek's Anthropic-compatible endpoint requires a thinking budget,
    # unlike the Responses API's effort-only input. Keep it below max_tokens
    # so the resulting Anthropic wire request is valid for budgeted thinking.
    budget = min(_REASONING_EFFORT_BUDGETS[effort], max_output_tokens - 1)
    return {"type": "enabled", "budget_tokens": budget}, True


def _reasoning_text(item: Mapping[str, object]) -> str | None:
    summary = item.get("summary")
    if isinstance(summary, list):
        texts = [
            value.get("text") for value in summary if isinstance(value, Mapping) and isinstance(value.get("text"), str)
        ]
        joined = "".join(texts)
        if joined.strip():
            return joined
    content = item.get("content")
    if isinstance(content, list):
        texts = [
            value.get("text") for value in content if isinstance(value, Mapping) and isinstance(value.get("text"), str)
        ]
        joined = "".join(texts)
        if joined.strip():
            return joined
    return item.get("text") if isinstance(item.get("text"), str) and item.get("text", "").strip() else None


def _text_blocks(value: object) -> list[dict[str, object]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, object]] = []
    for part in value:
        if isinstance(part, Mapping):
            if part.get("type") in {"input_text", "output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    blocks.append({"type": "text", "text": text})
    return blocks


def _function_input(item: Mapping[str, object]) -> object:
    arguments = item.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError as error:
            raise DeepSeekProtocolError("tool_history_invalid") from error
    return deepcopy(arguments)


def _append_function_call(
    messages: list[dict[str, object]],
    current_assistant: dict[str, object] | None,
    pending_reasoning: str | None,
    item: Mapping[str, object],
) -> tuple[dict[str, object], None]:
    call_id = item.get("call_id") or item.get("id")
    name = item.get("name")
    if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
        raise DeepSeekProtocolError("tool_history_invalid")
    assistant = current_assistant
    if assistant is None:
        assistant = {"role": "assistant", "content": []}
        messages.append(assistant)
        if pending_reasoning is not None:
            assistant["content"].append({"type": "thinking", "thinking": pending_reasoning})
    assistant["content"].append({"type": "tool_use", "id": call_id, "name": name, "input": _function_input(item)})
    return assistant, None


def _append_function_call_output(messages: list[dict[str, object]], item: Mapping[str, object]) -> None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        raise DeepSeekProtocolError("tool_history_invalid")
    messages.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": str(item.get("output", ""))}],
        }
    )


def _append_response_item(
    messages: list[dict[str, object]],
    item: Mapping[str, object],
    pending_reasoning: str | None,
) -> str | None:
    item_type = item.get("type")
    if item_type == "input_text":
        messages.append({"role": "user", "content": _text_blocks([item])})
        return pending_reasoning
    if item_type == "input_image":
        raise DeepSeekProtocolError("unsupported_input_item")
    if item_type != "message":
        raise DeepSeekProtocolError("unsupported_input_item")
    role = item.get("role")
    if role not in {"user", "assistant", "system"}:
        raise DeepSeekProtocolError("reasoning_input_invalid")
    content = _text_blocks(item.get("content"))
    if role == "assistant" and pending_reasoning is not None:
        content.insert(0, {"type": "thinking", "thinking": pending_reasoning})
        messages.append({"role": role, "content": content})
        return None
    messages.append({"role": role, "content": content})
    return pending_reasoning


def _responses_input_to_messages(
    input_value: str | ResponseInputParam,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if isinstance(input_value, str):
        return messages + [{"role": "user", "content": input_value}]
    pending_reasoning: str | None = None
    current_assistant: dict[str, object] | None = None
    for raw_item in input_value:
        if not isinstance(raw_item, Mapping):
            continue
        item_type = raw_item.get("type")
        if item_type == "reasoning":
            pending_reasoning = _reasoning_text(raw_item)
            continue
        if item_type == "function_call":
            current_assistant, pending_reasoning = _append_function_call(
                messages, current_assistant, pending_reasoning, raw_item
            )
            continue
        if item_type == "function_call_output":
            current_assistant = None
            _append_function_call_output(messages, raw_item)
            continue
        current_assistant = None
        pending_reasoning = _append_response_item(messages, raw_item, pending_reasoning)
    if pending_reasoning is not None:
        messages.append({"role": "assistant", "content": [{"type": "thinking", "thinking": pending_reasoning}]})
    return messages


def _responses_tools_to_anthropic(tools: object) -> list[dict[str, object]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") != "function":
            continue
        converted.append(
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": deepcopy(tool.get("parameters") or {"type": "object"}),
            }
        )
    return converted


def _responses_tool_choice_to_anthropic(tool_choice: object) -> dict[str, object] | None:
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return None
    if isinstance(tool_choice, Mapping):
        name = tool_choice.get("name")
        if name is None and isinstance(tool_choice.get("function"), Mapping):
            name = tool_choice["function"].get("name")
        if tool_choice.get("type") in {"function", "tool"} and isinstance(name, str) and name.strip():
            return {"type": "tool", "name": name}
    raise DeepSeekProtocolError("tool_choice_invalid")


def _system_prompt_from_messages(
    messages: tuple[dict[str, object], ...],
    instructions: object,
) -> tuple[tuple[dict[str, object], ...], object | None]:
    non_system: list[dict[str, object]] = []
    system_parts: list[object] = []
    if isinstance(instructions, str) and instructions:
        system_parts.append(instructions)
    for message in messages:
        if message.get("role") == "system":
            content = deepcopy(message.get("content", ""))
            if content:
                system_parts.append(content)
        else:
            non_system.append(message)
    if not system_parts:
        return tuple(non_system), None
    if len(system_parts) == 1:
        return tuple(non_system), system_parts[0]
    if all(isinstance(part, str) for part in system_parts):
        return tuple(non_system), "\n\n".join(part for part in system_parts if isinstance(part, str))
    blocks: list[object] = []
    for part in system_parts:
        if isinstance(part, list):
            blocks.extend(deepcopy(part))
        else:
            blocks.append({"type": "text", "text": str(part)})
    return tuple(non_system), blocks


async def _load_session_history(
    previous_response_id: object, session_repository: object
) -> tuple[dict[str, object], ...]:
    if not isinstance(previous_response_id, str):
        return ()
    load = getattr(session_repository, "load", None)
    if not callable(load):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    response_id = ResponsesAPIRequestUtils.decode_previous_response_id_to_original_previous_response_id(
        previous_response_id
    )
    session = await load(response_id)
    if session is None or not hasattr(session, "messages"):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    messages = getattr(session, "messages")
    if not isinstance(messages, tuple) or not all(isinstance(message, dict) for message in messages):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    return tuple(deepcopy(message) for message in messages)


def _session_repository_from_kwargs(kwargs: Mapping[str, object]) -> object:
    repository = kwargs.get("_deepseek_session_repository")
    return repository if repository is not None else SpendLogDeepSeekResponsesSessionRepository()


async def _stage_session(
    session_repository: object,
    proxy_server_request: object,
    response_id: str,
    messages: tuple[dict[str, object], ...],
    logging_obj: object | None = None,
) -> None:
    model_call_details = getattr(logging_obj, "model_call_details", None)
    if not isinstance(proxy_server_request, dict) and isinstance(model_call_details, dict):
        litellm_params = model_call_details.get("litellm_params")
        if isinstance(litellm_params, dict):
            proxy_server_request = litellm_params.get("proxy_server_request")
    requires_atomic = getattr(session_repository, "requires_atomic_session", False)
    supports_atomic = getattr(session_repository, "supports_atomic_session", True)
    session = create_deepseek_responses_session(response_id, messages, durability="atomic")
    if requires_atomic is True and supports_atomic is not True:
        # A suffix containing a tool call is not safely reconstructable from
        # SpendLog metadata. Do not hand out a response id that can never be
        # resumed after the request returns.
        if session.history_reasoning_required:
            raise DeepSeekProtocolError("reasoning_history_persistence_unavailable")
        return
    commit = getattr(session_repository, "commit", None)
    if not callable(commit):
        if session.history_reasoning_required:
            raise DeepSeekProtocolError("reasoning_history_persistence_unavailable")
        return
    try:
        result = commit(session)
        if isawaitable(result):
            await result
    except DeepSeekProtocolNonFallbackError:
        raise
    except Exception as error:
        raise DeepSeekProtocolError("reasoning_history_persistence_failed") from error
    payload = SpendLogDeepSeekResponsesSessionRepository.stage(proxy_server_request, response_id, messages)
    if not isinstance(model_call_details, dict):
        return
    model_call_details["deepseek_session_record"] = deepcopy(payload)
    litellm_params = model_call_details.get("litellm_params")
    if not isinstance(litellm_params, dict):
        return
    proxy_request = litellm_params.get("proxy_server_request")
    if not isinstance(proxy_request, dict):
        proxy_request = {}
    body = proxy_request.get("body")
    if not isinstance(body, dict):
        body = {}
    litellm_params["proxy_server_request"] = {
        **proxy_request,
        "body": {**body, "_deepseek_anthropic_session": deepcopy(payload)},
    }


def _bridge_optional_params(
    request: ResponsesAPIOptionalRequestParams,
    thinking: Mapping[str, object],
    enabled: bool,
    max_output_tokens: int,
) -> dict[str, object]:
    tools = _responses_tools_to_anthropic(request.get("tools"))
    params: dict[str, object] = {
        "max_tokens": max_output_tokens,
        "thinking": dict(thinking),
    }
    if tools:
        params["tools"] = tools
        tool_choice = request.get("tool_choice")
        if tool_choice is not None:
            translated_tool_choice = _responses_tool_choice_to_anthropic(tool_choice)
            if translated_tool_choice is not None:
                if request.get("parallel_tool_calls") is False:
                    translated_tool_choice["disable_parallel_tool_use"] = True
                params["tool_choice"] = translated_tool_choice
            else:
                # Anthropic has no wire-level ``none`` choice; omitting the
                # tools is the only lossless representation.
                params.pop("tools", None)
        elif request.get("parallel_tool_calls") is False:
            params["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
    elif request.get("tool_choice") not in (None, "none"):
        raise DeepSeekProtocolError("tool_choice_invalid")
    if request.get("temperature") is not None:
        params["temperature"] = request["temperature"]
    if request.get("top_p") is not None:
        params["top_p"] = request["top_p"]
    reasoning = request.get("reasoning")
    if enabled:
        effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else "high"
        if isinstance(effort, str) and effort in {"low", "high", "max"}:
            params["output_config"] = {"effort": effort}
    return params


def _http_client_from_kwargs(kwargs: Mapping[str, object]) -> tuple[httpx.AsyncClient, bool]:
    client = kwargs.get("client")
    if hasattr(client, "client") and isinstance(getattr(client, "client"), httpx.AsyncClient):
        return client.client, False
    if isinstance(client, httpx.AsyncClient):
        return client, False
    # Reuse the provider client's TLS/proxy/header configuration, while the
    # raw transport calls its underlying httpx client directly and therefore
    # cannot inherit AsyncHTTPHandler's retry or raise-for-status behavior.
    import litellm
    from litellm.llms.custom_httpx.http_handler import get_async_httpx_client

    return get_async_httpx_client(llm_provider=litellm.LlmProviders.ANTHROPIC).client, False


def _logging_safe_input(input_value: str | ResponseInputParam) -> str | ResponseInputParam:
    if not isinstance(input_value, list):
        return input_value
    return [
        deepcopy(item) for item in input_value if not (isinstance(item, Mapping) and item.get("type") == "reasoning")
    ]


def _log_parent_pre_call(logging_obj: object, input_value: str | ResponseInputParam) -> None:
    pre_call = getattr(logging_obj, "pre_call", None)
    if callable(pre_call):
        pre_call(input=_logging_safe_input(input_value), api_key="", additional_args={})


def _invalid_raw_payload_code(body: bytes, headers: Mapping[str, str]) -> str:
    if not body:
        return "upstream_response_empty"
    content_type = headers.get("content-type", "").lower()
    if "text/event-stream" in content_type or body.lstrip().startswith(b"data:"):
        return "upstream_response_unexpected_sse"
    if "text/html" in content_type or body.lstrip().startswith((b"<!doctype", b"<html", b"<HTML")):
        return "upstream_response_unexpected_html"
    return "upstream_response_invalid"


async def _read_raw_payload(response: httpx.Response, owns_client: bool, client: httpx.AsyncClient) -> object:
    body = b""
    try:
        body = await response.aread()
        try:
            # ``json.loads`` accepts bytes and handles a UTF-8 BOM. Some
            # Anthropic-compatible gateways emit one, while decoding first
            # makes otherwise valid JSON fail with ``Unexpected UTF-8 BOM``.
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeepSeekProtocolError(
                _invalid_raw_payload_code(body, dict(response.headers)),
                raw_headers=dict(response.headers),
                raw_body=body,
            ) from error
    finally:
        await response.aclose()
        if owns_client:
            await client.aclose()


def _anthropic_response_to_responses(
    payload: Mapping[str, object],
    model: str,
    previous_response_id: str | None,
    request: ResponsesAPIOptionalRequestParams,
    accounting: ParentAccounting,
) -> ResponsesAPIResponse:
    response_id = payload.get("id")
    content_value = payload.get("content")
    if (
        not isinstance(response_id, str)
        or not response_id.strip()
        or not isinstance(content_value, list)
        or isinstance(payload.get("error"), Mapping)
        or not all(isinstance(block, Mapping) and isinstance(block.get("type"), str) for block in content_value)
    ):
        raise DeepSeekProtocolError("upstream_response_invalid")
    content: list[Mapping[str, object]] = content_value
    output: list[dict[str, object]] = []
    assistant_content: list[dict[str, object]] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                output.append(
                    {
                        "type": "reasoning",
                        "id": f"rs_{response_id}",
                        "summary": [{"type": "summary_text", "text": thinking}],
                        "status": "completed",
                    }
                )
                assistant_content.append({"type": "thinking", "thinking": thinking})
        elif block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                output.append(
                    {
                        "type": "message",
                        "id": f"msg_{response_id}",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                )
                assistant_content.append({"type": "text", "text": text})
        elif block_type == "tool_use":
            call_id = block.get("id")
            if isinstance(call_id, str):
                arguments = json.dumps(block.get("input") or {}, separators=(",", ":"))
                output.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "call_id": call_id,
                        "name": block.get("name", ""),
                        "arguments": arguments,
                        "status": "completed",
                    }
                )
                assistant_content.append(deepcopy(dict(block)))
    response_status = "incomplete" if payload.get("stop_reason") in {"max_tokens", "length"} else "completed"
    response = ResponsesAPIResponse(
        id=response_id,
        created_at=int(time.time()),
        model=model,
        object="response",
        output=output,
        status=response_status,
        previous_response_id=previous_response_id,
        reasoning=request.get("reasoning") if isinstance(request.get("reasoning"), Mapping) else None,
        usage=_responses_usage(accounting),
    )
    if assistant_content:
        response._hidden_params["deepseek_assistant_content"] = assistant_content
    return response


def _accounting_rates(protocol_context: DeploymentProtocolContext) -> AttemptRateSnapshot:
    rates = protocol_context.rate_snapshot
    return AttemptRateSnapshot(
        input_cost_per_token=rates.input_cost_per_token,
        output_cost_per_token=rates.output_cost_per_token,
        cache_read_input_cost_per_token=rates.cache_read_input_cost_per_token,
        cache_creation_input_cost_per_token=rates.cache_creation_input_cost_per_token,
    )


def _parent_accounting(
    *,
    model: str,
    protocol_context: DeploymentProtocolContext,
    usage: Mapping[str, object],
    tracker: DeepSeekParentAccountingTracker,
) -> ParentAccounting:
    return tracker.record_attempt(
        build_attempt_snapshot(
            model=model,
            deployment_id=protocol_context.deployment_id,
            usage=usage,
            rates=_accounting_rates(protocol_context),
        )
    )


def _accounting_tracker(kwargs: Mapping[str, object]) -> DeepSeekParentAccountingTracker:
    tracker = kwargs.get("_deepseek_parent_accounting_tracker")
    return tracker if isinstance(tracker, DeepSeekParentAccountingTracker) else DeepSeekParentAccountingTracker()


def _router_owns_parent_accounting(kwargs: Mapping[str, object]) -> bool:
    return kwargs.get("_deepseek_parent_accounting_owner") is True


def _responses_usage(accounting: ParentAccounting) -> dict[str, object]:
    usage = accounting.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "input_tokens_details": {"cached_tokens": usage.cache_read_input_tokens},
        "cost": accounting.cost,
    }


def _apply_parent_accounting(
    response: ResponsesAPIResponse,
    accounting: ParentAccounting,
    logging_obj: object,
) -> None:
    summary = accounting.spend_log_summary()
    response.usage = ResponseAPIUsage(**_responses_usage(accounting))
    response._hidden_params["response_cost"] = accounting.cost
    response._hidden_params["deepseek_parent_accounting"] = summary
    _apply_parent_accounting_to_logging(accounting, logging_obj, response.usage)


def _apply_parent_accounting_to_logging(
    accounting: ParentAccounting,
    logging_obj: object,
    response_usage: object | None = None,
) -> None:
    summary = accounting.spend_log_summary()
    model_call_details = getattr(logging_obj, "model_call_details", None)
    if not isinstance(model_call_details, dict):
        return
    if response_usage is not None:
        model_call_details["combined_usage_object"] = (
            ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(response_usage)
        )
    model_call_details["response_cost"] = accounting.cost
    model_call_details["_deepseek_parent_accounting"] = True
    model_call_details["deepseek_parent_accounting"] = summary
    litellm_params = model_call_details.get("litellm_params")
    if not isinstance(litellm_params, dict):
        return
    metadata = litellm_params.get("metadata")
    metadata_values = dict(metadata) if isinstance(metadata, Mapping) else {}
    spend_logs_metadata = metadata_values.get("spend_logs_metadata")
    persisted_summary = {
        **(dict(spend_logs_metadata) if isinstance(spend_logs_metadata, Mapping) else {}),
        "deepseek_parent_accounting": summary,
    }
    litellm_params["metadata"] = {**metadata_values, "spend_logs_metadata": persisted_summary}


def _stream_terminal_response(
    payload: Mapping[str, object],
    *,
    model: str,
    previous_response_id: object,
    request: ResponsesAPIOptionalRequestParams,
    accounting: ParentAccounting,
) -> ResponsesAPIResponse:
    response_id = payload.get("id") if isinstance(payload.get("id"), str) else _new_deepseek_response_id()
    output = payload.get("output") if isinstance(payload.get("output"), list) else []
    status = payload.get("status") if isinstance(payload.get("status"), str) else "failed"
    return ResponsesAPIResponse(
        id=response_id,
        created_at=int(time.time()),
        model=model,
        object="response",
        output=deepcopy(output),
        status=status,
        previous_response_id=previous_response_id if isinstance(previous_response_id, str) else None,
        reasoning=request.get("reasoning") if isinstance(request.get("reasoning"), Mapping) else None,
        usage=_responses_usage(accounting),
    )


def _encoded_stream_response_id(
    response_id: str,
    *,
    protocol_context: DeploymentProtocolContext,
    custom_llm_provider: str | None,
) -> str:
    return ResponsesAPIRequestUtils._build_responses_api_response_id(
        model_id=protocol_context.deployment_id,
        custom_llm_provider=custom_llm_provider,
        response_id=response_id,
    )


async def _dispatch_parent_success(
    logging_obj: object,
    response: ResponsesAPIResponse,
    *,
    is_stream: bool,
) -> None:
    dispatch = getattr(logging_obj, "dispatch_success_handlers", None)
    if not callable(dispatch):
        return
    if is_stream:
        logging_obj.stream = False
    logging_response = response.model_copy()
    logging_response.output = [
        item
        for item in logging_response.output
        if (item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)) != "reasoning"
    ]
    logging_response._hidden_params = {}
    result = dispatch(logging_response)
    if isawaitable(result):
        await result


async def _dispatch_parent_failure(logging_obj: object, error: BaseException, is_async: bool) -> None:
    traceback_exception = "DeepSeek Responses stream terminal failure"
    start_time = getattr(logging_obj, "start_time", None)
    sanitized_error = _sanitize_failure_error(error)
    handler_name = "async_failure_handler" if is_async else "failure_handler"
    handler = getattr(logging_obj, handler_name, None)
    if not callable(handler):
        return
    result = handler(sanitized_error, traceback_exception, start_time, datetime.now())
    if isawaitable(result):
        await result


def _sanitize_failure_error(error: BaseException) -> BaseException:
    """Prevent preserved upstream evidence from reaching external log sinks."""
    if isinstance(error, DeepSeekProtocolError):
        return DeepSeekProtocolError(error.code)
    if isinstance(error, DeepSeekUpstreamError):
        return DeepSeekUpstreamError(error.category, error.status_code)
    return error


def _stream_event_value(event: object, field: str) -> object:
    if isinstance(event, Mapping):
        return event.get(field)
    return getattr(event, field, None)


def _response_from_stream_terminal(event: object) -> ResponsesAPIResponse | None:
    response = _stream_event_value(event, "response")
    if isinstance(response, ResponsesAPIResponse):
        return response
    if not isinstance(response, Mapping):
        return None
    response_id = response.get("id")
    output = response.get("output")
    if not isinstance(response_id, str) or not response_id or not isinstance(output, list):
        return None
    created_at = response.get("created_at")
    return ResponsesAPIResponse(
        id=response_id,
        created_at=created_at if isinstance(created_at, int) else int(time.time()),
        model=response.get("model") if isinstance(response.get("model"), str) else None,
        object=response.get("object") if isinstance(response.get("object"), str) else "response",
        output=deepcopy(output),
        status=response.get("status") if isinstance(response.get("status"), str) else None,
        usage=deepcopy(response.get("usage")) if isinstance(response.get("usage"), Mapping) else None,
    )


def _write_stream_terminal_response(event: object, response: ResponsesAPIResponse) -> None:
    terminal_response = _stream_event_value(event, "response")
    if isinstance(event, dict) and isinstance(terminal_response, Mapping):
        event["response"] = response.model_dump(exclude_none=True)


def _responses_output_to_assistant_content(output: object) -> list[dict[str, object]]:
    if not isinstance(output, list):
        return []
    content: list[dict[str, object]] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            thinking = _reasoning_text(item)
            if thinking is not None:
                content.append({"type": "thinking", "thinking": thinking})
        elif item_type == "message":
            content.extend(_text_blocks(item.get("content")))
        elif item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if isinstance(call_id, str) and call_id.strip() and isinstance(name, str) and name.strip():
                content.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": _function_input(item),
                    }
                )
    return content


class DeepSeekAnthropicResponsesBridge:
    @classmethod
    async def finalize_router_success(
        cls,
        *,
        tracker: DeepSeekParentAccountingTracker,
        response: ResponsesAPIResponse,
        logging_obj: object,
    ) -> None:
        if not tracker.claim_lifecycle():
            return
        _apply_parent_accounting(response, tracker.accounting, logging_obj)
        await _dispatch_parent_success(logging_obj, response, is_stream=False)

    @classmethod
    async def finalize_router_failure(
        cls,
        *,
        tracker: DeepSeekParentAccountingTracker,
        logging_obj: object,
        error: BaseException,
        is_async: bool,
    ) -> None:
        if not tracker.claim_lifecycle():
            return
        _apply_parent_accounting_to_logging(tracker.accounting, logging_obj)
        await _dispatch_parent_failure(logging_obj, error, is_async)

    @classmethod
    async def finalize_router_response(
        cls,
        *,
        tracker: DeepSeekParentAccountingTracker,
        response: ResponsesAPIResponse,
        logging_obj: object,
        is_async: bool,
    ) -> None:
        """Finalize a non-stream response according to its wire terminal state."""
        if getattr(response, "status", None) == "completed":
            await cls.finalize_router_success(
                tracker=tracker,
                response=response,
                logging_obj=logging_obj,
            )
            return
        await cls.finalize_router_failure(
            tracker=tracker,
            logging_obj=logging_obj,
            error=DeepSeekUpstreamError("response_incomplete", None),
            is_async=is_async,
        )

    @classmethod
    async def finalize_router_stream_terminal(
        cls,
        *,
        tracker: DeepSeekParentAccountingTracker,
        event: object,
        logging_obj: object,
        is_async: bool,
    ) -> None:
        """Finish the one parent lifecycle when a native fallback stream ends."""
        event_type = _stream_event_value(event, "type")
        response = _response_from_stream_terminal(event)
        if event_type == "response.completed" and response is not None:
            await cls.finalize_router_success(
                tracker=tracker,
                response=response,
                logging_obj=logging_obj,
            )
            _write_stream_terminal_response(event, response)
            return
        await cls.finalize_router_failure(
            tracker=tracker,
            logging_obj=logging_obj,
            error=DeepSeekUpstreamError(
                "stream_incomplete" if event_type == "response.incomplete" else "stream_failed",
                None,
            ),
            is_async=is_async,
        )

    @classmethod
    def response_api_handler(
        cls,
        *,
        model: str,
        input: str | ResponseInputParam,
        responses_api_request: ResponsesAPIOptionalRequestParams,
        custom_llm_provider: str | None,
        _is_async: bool,
        stream: bool | None,
        protocol_context: DeploymentProtocolContext,
        **kwargs: object,
    ) -> object:
        if not protocol_context.is_router_provenanced():
            raise DeepSeekProtocolError("router_provenance_required")
        if _is_async:
            return cls._async_handle(
                model=model,
                input=input,
                responses_api_request=responses_api_request,
                custom_llm_provider=custom_llm_provider,
                stream=stream,
                protocol_context=protocol_context,
                is_async=True,
                kwargs=kwargs,
            )
        if stream:
            return DeepSeekAnthropicResponsesSyncStream(
                cls._async_handle(
                    model=model,
                    input=input,
                    responses_api_request=responses_api_request,
                    custom_llm_provider=custom_llm_provider,
                    stream=True,
                    protocol_context=protocol_context,
                    is_async=False,
                    kwargs=kwargs,
                ),
                model=model,
                pre_output_fallback_enabled=kwargs.get("_deepseek_pre_output_stream_fallback") is True,
            )
        return run_async_function(
            cls._async_handle,
            model=model,
            input=input,
            responses_api_request=responses_api_request,
            custom_llm_provider=custom_llm_provider,
            stream=stream,
            protocol_context=protocol_context,
            is_async=False,
            kwargs=kwargs,
        )

    @classmethod
    async def _async_handle(
        cls,
        *,
        model: str,
        input: str | ResponseInputParam,
        responses_api_request: ResponsesAPIOptionalRequestParams,
        custom_llm_provider: str | None,
        stream: bool | None,
        protocol_context: DeploymentProtocolContext,
        is_async: bool,
        kwargs: Mapping[str, object],
    ) -> object:
        reasoning = responses_api_request.get("reasoning")
        max_output_tokens = _resolved_max_output_tokens(responses_api_request)
        thinking, enabled = _effort_to_thinking(reasoning, max_output_tokens=max_output_tokens)
        previous_response_id = responses_api_request.get("previous_response_id")
        session_repository = _session_repository_from_kwargs(kwargs)
        session_history = await _load_session_history(previous_response_id, session_repository)
        new_messages = _responses_input_to_messages(input)
        all_messages = session_history + tuple(new_messages)
        all_messages, system_prompt = _system_prompt_from_messages(
            all_messages, responses_api_request.get("instructions")
        )
        canonical = compile_deepseek_anthropic_history(
            all_messages,
            thinking,
            max_suffix_tokens=protocol_context.suffix_token_budget,
        )
        optional_params = _bridge_optional_params(
            responses_api_request,
            thinking,
            enabled,
            max_output_tokens,
        )
        # Preserve the native Messages serializer's field ordering. Some
        # Anthropic-compatible gateways inspect the signed JSON bytes.
        optional_params["stream"] = stream is True
        optional_params["_deepseek_reasoning_suffix_token_budget"] = protocol_context.suffix_token_budget
        optional_params["_deepseek_reasoning_context_token_budget"] = protocol_context.context_token_budget
        config = DeepSeekAnthropicMessagesConfig()
        api_key = kwargs.get("api_key") if isinstance(kwargs.get("api_key"), str) else None
        api_base = kwargs.get("api_base") if isinstance(kwargs.get("api_base"), str) else None
        forwarded_headers = kwargs.get("headers")
        extra_headers = kwargs.get("extra_headers")
        provider_specific_headers = ProviderSpecificHeaderUtils.get_provider_specific_headers(
            provider_specific_header=kwargs.get("provider_specific_header"),
            custom_llm_provider=custom_llm_provider,
        )
        request_headers: dict[str, object] = {}
        if isinstance(forwarded_headers, Mapping):
            request_headers.update(forwarded_headers)
        if isinstance(extra_headers, Mapping):
            request_headers.update(extra_headers)
        if provider_specific_headers:
            request_headers.update(provider_specific_headers)
        headers, resolved_base = config.validate_anthropic_messages_environment(
            headers=request_headers,
            model=model,
            messages=list(canonical.messages),
            optional_params=optional_params,
            litellm_params=dict(kwargs),
            api_key=api_key,
            api_base=api_base,
        )
        if config.should_filter_anthropic_beta_headers():
            headers = update_headers_with_filtered_beta(headers=headers, provider=custom_llm_provider)
        request_body = config.transform_anthropic_messages_request(
            model=model,
            messages=list(canonical.messages),
            anthropic_messages_optional_request_params=optional_params,
            litellm_params=GenericLiteLLMParams(**dict(kwargs)),
            headers=headers,
        )
        if system_prompt is not None:
            request_body["system"] = system_prompt
        url = config.get_complete_url(
            api_base=resolved_base,
            api_key=api_key,
            model=model,
            optional_params=optional_params,
            litellm_params=dict(kwargs),
        )
        headers, signed_body = config.sign_request(
            headers=headers,
            optional_params=dict(kwargs),
            request_data=request_body,
            api_base=url,
            api_key=api_key,
            stream=stream,
            fake_stream=False,
            model=model,
        )
        http_client, owns_client = _http_client_from_kwargs(kwargs)
        accounting_tracker = _accounting_tracker(kwargs)
        router_owns_accounting = _router_owns_parent_accounting(kwargs)
        _log_parent_pre_call(kwargs.get("litellm_logging_obj"), input)
        raw_result = await DeepSeekResponsesRawTransport(http_client).send(
            freeze_deepseek_request(
                url=url,
                headers=headers,
                body=signed_body if signed_body is not None else request_body,
                stream=stream is True,
            )
        )
        if isinstance(raw_result, DeepSeekRawFailure):
            if owns_client:
                await http_client.aclose()
            _parent_accounting(
                model=model,
                protocol_context=protocol_context,
                usage={},
                tracker=accounting_tracker,
            )
            error = _raw_failure_exception(raw_result)
            if not router_owns_accounting:
                await cls.finalize_router_failure(
                    tracker=accounting_tracker,
                    logging_obj=kwargs.get("litellm_logging_obj"),
                    error=error,
                    is_async=is_async,
                )
            raise error
        if stream:
            raw_response_id = _new_deepseek_response_id()
            response_id = _encoded_stream_response_id(
                raw_response_id,
                protocol_context=protocol_context,
                custom_llm_provider=custom_llm_provider,
            )

            async def handle_stream_terminal(event: Mapping[str, object], output_started: bool) -> None:
                raw_response = event.get("response")
                if not isinstance(raw_response, Mapping):
                    return
                raw_usage = raw_response.get("usage") if isinstance(raw_response.get("usage"), Mapping) else {}
                accounting = _parent_accounting(
                    model=model,
                    protocol_context=protocol_context,
                    usage=raw_usage,
                    tracker=accounting_tracker,
                )
                response = _stream_terminal_response(
                    raw_response,
                    model=model,
                    previous_response_id=previous_response_id,
                    request=responses_api_request,
                    accounting=accounting,
                )
                logging_obj = kwargs.get("litellm_logging_obj")
                _apply_parent_accounting(response, accounting, logging_obj)
                _write_stream_terminal_response(event, response)
                if event.get("type") != "response.completed":
                    local_cancellation = event.get("_local_cancellation") is True
                    if not (router_owns_accounting and not output_started and not local_cancellation):
                        await cls.finalize_router_failure(
                            tracker=accounting_tracker,
                            logging_obj=logging_obj,
                            error=DeepSeekUpstreamError(
                                "stream_incomplete" if event.get("type") == "response.incomplete" else "stream_failed",
                                None,
                            ),
                            is_async=is_async,
                        )
                    return
                assistant_content = _responses_output_to_assistant_content(raw_response.get("output"))
                if assistant_content:
                    session_messages = list(canonical.messages)
                    session_messages.append({"role": "assistant", "content": assistant_content})
                    try:
                        await _stage_session(
                            session_repository,
                            kwargs.get("proxy_server_request"),
                            raw_response_id,
                            tuple(session_messages),
                            kwargs.get("litellm_logging_obj"),
                        )
                    except DeepSeekProtocolNonFallbackError as error:
                        if not router_owns_accounting:
                            await cls.finalize_router_failure(
                                tracker=accounting_tracker,
                                logging_obj=logging_obj,
                                error=error,
                                is_async=is_async,
                            )
                        raise
                if accounting_tracker.claim_lifecycle():
                    await _dispatch_parent_success(logging_obj, response, is_stream=True)

            return DeepSeekAnthropicResponsesAsyncStream(
                raw_result.response,
                model,
                response_id,
                owns_client,
                http_client,
                handle_stream_terminal,
                kwargs.get("_deepseek_pre_output_stream_fallback") is True,
            )
        try:
            payload = await _read_raw_payload(raw_result.response, owns_client, http_client)
            if not isinstance(payload, Mapping):
                raise DeepSeekProtocolError("upstream_response_invalid")
        except DeepSeekProtocolNonFallbackError as error:
            _parent_accounting(
                model=model,
                protocol_context=protocol_context,
                usage={},
                tracker=accounting_tracker,
            )
            if not router_owns_accounting:
                await cls.finalize_router_failure(
                    tracker=accounting_tracker,
                    logging_obj=kwargs.get("litellm_logging_obj"),
                    error=error,
                    is_async=is_async,
                )
            raise
        except Exception as error:
            protocol_error = DeepSeekProtocolError("upstream_response_invalid")
            _parent_accounting(
                model=model,
                protocol_context=protocol_context,
                usage={},
                tracker=accounting_tracker,
            )
            if not router_owns_accounting:
                await cls.finalize_router_failure(
                    tracker=accounting_tracker,
                    logging_obj=kwargs.get("litellm_logging_obj"),
                    error=protocol_error,
                    is_async=is_async,
                )
            raise protocol_error from error
        raw_usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        accounting = _parent_accounting(
            model=model,
            protocol_context=protocol_context,
            usage=raw_usage,
            tracker=accounting_tracker,
        )
        try:
            response_obj = _anthropic_response_to_responses(
                payload,
                model,
                previous_response_id,
                responses_api_request,
                accounting,
            )
        except DeepSeekProtocolNonFallbackError as error:
            if not router_owns_accounting:
                await cls.finalize_router_failure(
                    tracker=accounting_tracker,
                    logging_obj=kwargs.get("litellm_logging_obj"),
                    error=error,
                    is_async=is_async,
                )
            raise
        except Exception as error:
            protocol_error = DeepSeekProtocolError("upstream_response_invalid")
            if not router_owns_accounting:
                await cls.finalize_router_failure(
                    tracker=accounting_tracker,
                    logging_obj=kwargs.get("litellm_logging_obj"),
                    error=protocol_error,
                    is_async=is_async,
                )
            raise protocol_error from error
        # Router-owned requests defer dispatch until the fallback engine has
        # returned. Stamp the result now so Router can identify the DeepSeek
        # bridge response and finalize the single parent lifecycle.
        _apply_parent_accounting(response_obj, accounting, kwargs.get("litellm_logging_obj"))
        assistant_content = response_obj._hidden_params.get("deepseek_assistant_content")
        if response_obj.status == "completed" and isinstance(assistant_content, list):
            session_messages = list(canonical.messages)
            session_messages.append({"role": "assistant", "content": assistant_content})
            try:
                await _stage_session(
                    session_repository,
                    kwargs.get("proxy_server_request"),
                    response_obj.id,
                    tuple(session_messages),
                    kwargs.get("litellm_logging_obj"),
                )
            except DeepSeekProtocolNonFallbackError as error:
                if not router_owns_accounting:
                    await cls.finalize_router_failure(
                        tracker=accounting_tracker,
                        logging_obj=kwargs.get("litellm_logging_obj"),
                        error=error,
                        is_async=is_async,
                    )
                raise
        if not router_owns_accounting:
            await cls.finalize_router_response(
                tracker=accounting_tracker,
                response=response_obj,
                logging_obj=kwargs.get("litellm_logging_obj"),
                is_async=is_async,
            )
        return response_obj


__all__ = ["DeepSeekAnthropicResponsesBridge"]
