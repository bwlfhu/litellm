"""Responses bridge for a Router-authorized DeepSeek Anthropic deployment."""

import json
import time
from copy import deepcopy
from datetime import datetime
from inspect import isawaitable
from typing import Mapping, NoReturn

import httpx
from pydantic import TypeAdapter, ValidationError

from litellm.litellm_core_utils.asyncify import run_async_function
from litellm.llms.deepseek.anthropic_protocol import (
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
    ParentAccounting,
    build_attempt_snapshot,
)
from litellm.responses.deepseek_session import SpendLogDeepSeekResponsesSessionRepository
from litellm.responses.deepseek_streaming import (
    DeepSeekAnthropicResponsesAsyncStream,
    DeepSeekAnthropicResponsesSyncStream,
)
from litellm.responses.utils import ResponseAPILoggingUtils
from litellm.router_protocol import DeploymentProtocolContext
from litellm.types.llms.openai import ResponseInputParam, ResponsesAPIOptionalRequestParams, ResponsesAPIResponse
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
    }
)
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, object])


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
    return None


def _raise_raw_failure(failure: DeepSeekRawFailure) -> NoReturn:
    code = _error_code_from_upstream_body(failure.body)
    if code is not None:
        raise DeepSeekProtocolError(code)
    raise DeepSeekUpstreamError(failure.category, failure.status_code)


def _effort_to_thinking(reasoning: object) -> tuple[dict[str, object], bool]:
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
    return {"type": "enabled"}, True


def _reasoning_text(item: Mapping[str, object]) -> str | None:
    summary = item.get("summary")
    if isinstance(summary, list):
        texts = [
            value.get("text")
            for value in summary
            if isinstance(value, Mapping) and isinstance(value.get("text"), str)
        ]
        joined = "".join(texts)
        if joined.strip():
            return joined
    content = item.get("content")
    if isinstance(content, list):
        texts = [
            value.get("text")
            for value in content
            if isinstance(value, Mapping) and isinstance(value.get("text"), str)
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
    if item_type in {"input_text", "input_image"}:
        messages.append({"role": "user", "content": _text_blocks([item])})
        return pending_reasoning
    if item_type != "message":
        return pending_reasoning
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
    instructions: object,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
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


async def _load_session_history(
    previous_response_id: object, session_repository: object
) -> tuple[dict[str, object], ...]:
    if not isinstance(previous_response_id, str):
        return ()
    load = getattr(session_repository, "load", None)
    if not callable(load):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    session = await load(previous_response_id)
    if session is None or not hasattr(session, "messages"):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    messages = getattr(session, "messages")
    if not isinstance(messages, tuple) or not all(isinstance(message, dict) for message in messages):
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    return tuple(deepcopy(message) for message in messages)


def _session_repository_from_kwargs(kwargs: Mapping[str, object]) -> object:
    repository = kwargs.get("_deepseek_session_repository")
    return repository if repository is not None else SpendLogDeepSeekResponsesSessionRepository()


def _stage_session(
    session_repository: object,
    proxy_server_request: object,
    response_id: str,
    messages: tuple[dict[str, object], ...],
) -> None:
    stage = getattr(session_repository, "stage", None)
    if callable(stage):
        stage(proxy_server_request, response_id, messages)


def _bridge_optional_params(
    request: ResponsesAPIOptionalRequestParams,
    thinking: Mapping[str, object],
    enabled: bool,
) -> dict[str, object]:
    tools = _responses_tools_to_anthropic(request.get("tools"))
    params: dict[str, object] = {
        "max_tokens": request.get("max_output_tokens") or 1024,
        "thinking": dict(thinking),
    }
    if tools:
        params["tools"] = tools
    reasoning = request.get("reasoning")
    if enabled and isinstance(reasoning, Mapping) and reasoning.get("effort") in {"low", "high", "max"}:
        params["output_config"] = {"effort": reasoning["effort"]}
    return params


def _http_client_from_kwargs(kwargs: Mapping[str, object]) -> tuple[httpx.AsyncClient, bool]:
    client = kwargs.get("client")
    if hasattr(client, "client") and isinstance(getattr(client, "client"), httpx.AsyncClient):
        return client.client, False
    if isinstance(client, httpx.AsyncClient):
        return client, False
    return httpx.AsyncClient(), True


def _log_parent_pre_call(logging_obj: object, input_value: str | ResponseInputParam) -> None:
    pre_call = getattr(logging_obj, "pre_call", None)
    if callable(pre_call):
        pre_call(input=input_value, api_key="", additional_args={})


async def _read_raw_payload(response: httpx.Response, owns_client: bool, client: httpx.AsyncClient) -> object:
    try:
        return json.loads((await response.aread()).decode())
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
    response_id = payload.get("id") if isinstance(payload.get("id"), str) else f"resp_ds_{int(time.time() * 1000)}"
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
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
) -> ParentAccounting:
    return ParentAccounting().add_attempt(
        build_attempt_snapshot(
            model=model,
            deployment_id=protocol_context.deployment_id,
            usage=usage,
            rates=_accounting_rates(protocol_context),
        )
    )


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
    response._hidden_params["response_cost"] = accounting.cost
    response._hidden_params["deepseek_parent_accounting"] = summary
    model_call_details = getattr(logging_obj, "model_call_details", None)
    if not isinstance(model_call_details, dict):
        return
    model_call_details["combined_usage_object"] = ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(
        response.usage
    )
    model_call_details["response_cost"] = accounting.cost
    model_call_details["deepseek_parent_accounting"] = summary


def _stream_terminal_response(
    payload: Mapping[str, object],
    *,
    model: str,
    previous_response_id: object,
    request: ResponsesAPIOptionalRequestParams,
    accounting: ParentAccounting,
) -> ResponsesAPIResponse:
    response_id = payload.get("id") if isinstance(payload.get("id"), str) else f"resp_ds_{int(time.time() * 1000)}"
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
    result = dispatch(response)
    if isawaitable(result):
        await result


async def _dispatch_stream_failure(logging_obj: object, category: str, is_async: bool) -> None:
    error = DeepSeekUpstreamError(category, None)
    traceback_exception = "DeepSeek Responses stream terminal failure"
    start_time = getattr(logging_obj, "start_time", None)
    failure_handler = getattr(logging_obj, "failure_handler", None)
    if callable(failure_handler):
        failure_handler(error, traceback_exception, start_time, datetime.now())
    if not is_async:
        return
    async_failure_handler = getattr(logging_obj, "async_failure_handler", None)
    if not callable(async_failure_handler):
        return
    result = async_failure_handler(error, traceback_exception, start_time, datetime.now())
    if isawaitable(result):
        await result


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
                )
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
        del custom_llm_provider
        reasoning = responses_api_request.get("reasoning")
        thinking, enabled = _effort_to_thinking(reasoning)
        previous_response_id = responses_api_request.get("previous_response_id")
        session_repository = _session_repository_from_kwargs(kwargs)
        session_history = await _load_session_history(previous_response_id, session_repository)
        new_messages = _responses_input_to_messages(input, responses_api_request.get("instructions"))
        all_messages = session_history + tuple(new_messages)
        canonical = compile_deepseek_anthropic_history(
            all_messages,
            thinking,
            max_suffix_tokens=protocol_context.suffix_token_budget,
        )
        optional_params = _bridge_optional_params(responses_api_request, thinking, enabled)
        optional_params["_deepseek_reasoning_suffix_token_budget"] = protocol_context.suffix_token_budget
        optional_params["_deepseek_reasoning_context_token_budget"] = protocol_context.suffix_token_budget
        config = DeepSeekAnthropicMessagesConfig()
        request_body = config.transform_anthropic_messages_request(
            model=model,
            messages=list(canonical.messages),
            anthropic_messages_optional_request_params=optional_params,
            litellm_params=GenericLiteLLMParams(**dict(kwargs)),
            headers={},
        )
        api_key = kwargs.get("api_key") if isinstance(kwargs.get("api_key"), str) else None
        api_base = kwargs.get("api_base") if isinstance(kwargs.get("api_base"), str) else None
        headers, resolved_base = config.validate_anthropic_messages_environment(
            headers=dict(kwargs.get("headers", {})) if isinstance(kwargs.get("headers"), Mapping) else {},
            model=model,
            messages=list(canonical.messages),
            optional_params=optional_params,
            litellm_params=dict(kwargs),
            api_key=api_key,
            api_base=api_base,
        )
        url = config.get_complete_url(
            api_base=resolved_base,
            api_key=api_key,
            model=model,
            optional_params=optional_params,
            litellm_params=dict(kwargs),
        )
        http_client, owns_client = _http_client_from_kwargs(kwargs)
        _log_parent_pre_call(kwargs.get("litellm_logging_obj"), input)
        raw_result = await DeepSeekResponsesRawTransport(http_client).send(
            freeze_deepseek_request(url=url, headers=headers, body=request_body, stream=stream is True)
        )
        if isinstance(raw_result, DeepSeekRawFailure):
            if owns_client:
                await http_client.aclose()
            _raise_raw_failure(raw_result)
        if stream:
            response_id = f"resp_ds_{int(time.time() * 1000)}"

            async def handle_stream_terminal(event: Mapping[str, object]) -> None:
                raw_response = event.get("response")
                if not isinstance(raw_response, Mapping):
                    return
                raw_usage = raw_response.get("usage") if isinstance(raw_response.get("usage"), Mapping) else {}
                accounting = _parent_accounting(
                    model=model,
                    protocol_context=protocol_context,
                    usage=raw_usage,
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
                if event.get("type") != "response.completed":
                    await _dispatch_stream_failure(
                        logging_obj,
                        "stream_incomplete" if event.get("type") == "response.incomplete" else "stream_failed",
                        is_async,
                    )
                    return
                assistant_content = _responses_output_to_assistant_content(raw_response.get("output"))
                if assistant_content:
                    session_messages = list(canonical.messages)
                    session_messages.append({"role": "assistant", "content": assistant_content})
                    _stage_session(
                        session_repository,
                        kwargs.get("proxy_server_request"),
                        response_id,
                        tuple(session_messages),
                    )
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
        payload = await _read_raw_payload(raw_result.response, owns_client, http_client)
        if not isinstance(payload, Mapping):
            raise DeepSeekProtocolError("upstream_response_invalid")
        raw_usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        accounting = _parent_accounting(
            model=model,
            protocol_context=protocol_context,
            usage=raw_usage,
        )
        response_obj = _anthropic_response_to_responses(
            payload,
            model,
            previous_response_id,
            responses_api_request,
            accounting,
        )
        _apply_parent_accounting(response_obj, accounting, kwargs.get("litellm_logging_obj"))
        assistant_content = response_obj._hidden_params.get("deepseek_assistant_content")
        if response_obj.status == "completed" and isinstance(assistant_content, list):
            session_messages = list(canonical.messages)
            session_messages.append({"role": "assistant", "content": assistant_content})
            _stage_session(
                session_repository,
                kwargs.get("proxy_server_request"),
                response_obj.id,
                tuple(session_messages),
            )
        await _dispatch_parent_success(kwargs.get("litellm_logging_obj"), response_obj, is_stream=False)
        return response_obj


__all__ = ["DeepSeekAnthropicResponsesBridge"]
