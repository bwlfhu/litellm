import json
import os
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest


from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
    anthropic_messages_handler,
)
from litellm.llms.anthropic.experimental_pass_through.messages.mcp_handler import (
    _build_tool_result_message,
    _extract_tool_use_blocks,
)
from litellm.llms.deepseek.messages.transformation import DeepSeekAnthropicMessagesConfig
from litellm.router_protocol import _build_deployment_protocol_context

MCP_REFERENCE = {
    "type": "mcp",
    "server_label": "litellm",
    "server_url": "litellm_proxy/mcp/deepwiki",
    "require_approval": "never",
}


@pytest.mark.asyncio
async def test_anthropic_messages_handler_routes_litellm_proxy_mcp_to_the_gateway():
    """
    Regression test (LIT-4517): /v1/messages must expand a litellm_proxy MCP
    reference through the MCP gateway.

    Given: A /v1/messages request whose tools carry a litellm_proxy MCP reference
    When:  The handler dispatches
    Then:  It hands off to the MCP gateway instead of the provider

    Without this hook the reference is forwarded to Anthropic verbatim and the API
    rejects the request ("Input tag 'mcp' found using 'type' does not match any of
    the expected tags"), because only /v1/chat/completions and /v1/responses ever
    had a gateway entry point. This pins the wiring, not the helper: deleting the
    dispatch makes the whole feature unreachable while every unit test still passes.
    """
    with patch(
        "litellm.llms.anthropic.experimental_pass_through.messages.mcp_handler.anthropic_messages_with_mcp",
        new=AsyncMock(return_value={"routed": True}),
    ) as routed:
        result = await anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-5",
            tools=[MCP_REFERENCE],
            custom_llm_provider="anthropic",
        )

    assert routed.called, "A litellm_proxy MCP reference must be dispatched to the MCP gateway"
    assert routed.call_args.kwargs["tools"] == [MCP_REFERENCE]
    assert routed.call_args.kwargs["model"] == "claude-sonnet-4-5"
    assert result is not None


@pytest.mark.asyncio
async def test_anthropic_messages_handler_preserves_trusted_protocol_context_for_mcp_recursion():
    protocol_context = _build_deployment_protocol_context(
        {
            "deepseek_anthropic_messages_path": "v1/messages",
            "deepseek_anthropic_tool_thinking": "disabled",
        }
    )
    assert protocol_context is not None

    with patch(
        "litellm.llms.anthropic.experimental_pass_through.messages.mcp_handler.anthropic_messages_with_mcp",
        new=AsyncMock(return_value={"routed": True}),
    ) as routed:
        result = await anthropic_messages_handler(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-test",
            tools=[MCP_REFERENCE],
            custom_llm_provider="anthropic",
            _litellm_deployment_protocol_context=protocol_context,
        )

    assert result is not None
    assert routed.call_args.kwargs["_litellm_deployment_protocol_context"] is protocol_context


@pytest.mark.asyncio
async def test_anthropic_messages_with_mcp_preserves_protocol_context_on_recursive_call():
    from litellm.llms.anthropic.experimental_pass_through.messages import mcp_handler

    protocol_context = _build_deployment_protocol_context({"deepseek_anthropic_messages_path": "v1/messages"})
    assert protocol_context is not None
    recursive_call = AsyncMock(return_value={"stop_reason": "end_turn", "content": []})

    with patch("litellm.anthropic_messages", new=recursive_call):
        await mcp_handler.anthropic_messages_with_mcp(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="claude-test",
            tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
            _litellm_deployment_protocol_context=protocol_context,
        )

    assert recursive_call.await_args.kwargs["_litellm_deployment_protocol_context"] is protocol_context


@pytest.mark.asyncio
async def test_mcp_recursive_protocol_context_stops_at_provider_boundary():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler, mcp_handler

    protocol_context = _build_deployment_protocol_context({"deepseek_anthropic_messages_path": "v1/messages"})
    assert protocol_context is not None
    logging_obj = Logging(
        model="claude-test",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
        call_type="anthropic_messages",
        start_time=time.time(),
        litellm_call_id="mcp-protocol-call",
        function_id="mcp-protocol-function",
    )

    try:
        with patch.object(
            handler.base_llm_http_handler, "anthropic_messages_handler", return_value={"content": []}
        ) as dispatch:
            await mcp_handler.anthropic_messages_with_mcp(
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                model="anthropic/claude-test",
                tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
                custom_llm_provider="anthropic",
                api_base="https://provider.example.test/anthropic/v1/messages",
                litellm_logging_obj=logging_obj,
                _litellm_deployment_protocol_context=protocol_context,
            )
    finally:
        await GLOBAL_LOGGING_WORKER.stop()

    dispatched = dispatch.call_args.kwargs
    config = dispatched["anthropic_messages_provider_config"]
    request_body = config.transform_anthropic_messages_request(
        model=dispatched["model"],
        messages=dispatched["messages"],
        anthropic_messages_optional_request_params=dispatched["anthropic_messages_optional_request_params"],
        litellm_params=dispatched["litellm_params"],
        headers={},
    )

    assert isinstance(config, DeepSeekAnthropicMessagesConfig)
    assert (
        config.get_complete_url(
            api_base=dispatched["api_base"],
            api_key=None,
            model=dispatched["model"],
            optional_params={},
            litellm_params={},
        )
        == "https://provider.example.test/v1/messages"
    )
    assert "_litellm_deployment_protocol_context" not in dispatched["kwargs"]
    assert "_litellm_deployment_protocol_context" not in dispatched["anthropic_messages_optional_request_params"]
    assert "_litellm_deployment_protocol_context" not in dispatched["litellm_params"].model_dump()
    assert "_litellm_deployment_protocol_context" not in logging_obj.litellm_params
    assert "_litellm_deployment_protocol_context" not in logging_obj.model_call_details.get("litellm_params", {})
    assert "_litellm_deployment_protocol_context" not in json.dumps(logging_obj.litellm_params)
    assert "_litellm_deployment_protocol_context" not in json.dumps(request_body)


def test_anthropic_messages_handler_skips_the_gateway_on_recursion():
    """The gateway's own follow-up call must not re-enter the gateway."""
    with patch(
        "litellm.llms.anthropic.experimental_pass_through.messages.mcp_handler.anthropic_messages_with_mcp",
        new=AsyncMock(return_value={"routed": True}),
    ) as routed:
        with pytest.raises(ValueError, match="anthropic_messages_handler is not implemented for sync calls"):
            anthropic_messages_handler(
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-5",
                tools=[MCP_REFERENCE],
                custom_llm_provider="anthropic",
                _skip_mcp_handler=True,
            )

    assert not routed.called, "_skip_mcp_handler must stop the gateway from recursing"


def test_anthropic_messages_handler_leaves_native_tools_alone():
    """A plain Anthropic tool is not an MCP reference and must not reach the gateway."""
    with patch(
        "litellm.llms.anthropic.experimental_pass_through.messages.mcp_handler.anthropic_messages_with_mcp",
        new=AsyncMock(return_value={"routed": True}),
    ) as routed:
        with pytest.raises(ValueError, match="anthropic_messages_handler is not implemented for sync calls"):
            anthropic_messages_handler(
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                model="claude-sonnet-4-5",
                tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
                custom_llm_provider="anthropic",
            )

    assert not routed.called, "Only litellm_proxy MCP references belong to the gateway"


def test_extract_tool_use_blocks_ignores_text_blocks():
    """Only tool_use blocks drive the loop; text blocks are the model's prose."""
    response = {
        "content": [
            {"type": "text", "text": "let me look that up"},
            {"type": "tool_use", "id": "toolu_1", "name": "read_wiki_structure", "input": {"repoName": "a/b"}},
        ]
    }

    blocks = _extract_tool_use_blocks(response)

    assert len(blocks) == 1
    assert blocks[0]["name"] == "read_wiki_structure"


def test_build_tool_result_message_uses_anthropic_tool_result_blocks():
    """
    Results must go back as tool_result blocks in a user message.

    Anthropic pairs each result to its request by tool_use_id; the OpenAI shape
    (a role="tool" message keyed by tool_call_id) is rejected here.
    """
    message = _build_tool_result_message([{"tool_call_id": "toolu_1", "result": "9 sections", "name": "read_wiki"}])

    assert message["role"] == "user"
    assert list(message["content"]) == [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "9 sections"}]


@pytest.mark.asyncio
async def test_anthropic_messages_with_mcp_forwards_the_callers_mcp_credentials():
    """
    Regression test (LIT-4517): the caller's MCP auth must reach both tool listing
    and tool execution on /v1/messages.

    Given: A request carrying MCP auth headers and request tags
    When:  The gateway lists and then executes an MCP tool
    Then:  Both calls receive the caller's credentials, tags and trace ids

    Dropping them does not fail loudly; the tool still executes, just with no
    credentials, so every auth-requiring MCP server (interactive OAuth, bearer
    token, per-user env) silently returns nothing while the model claims it has
    no access. Only a no-auth server would look healthy.
    """
    from litellm.llms.anthropic.experimental_pass_through.messages import mcp_handler
    from litellm.responses.mcp.request_context import MCPRequestContext

    context = MCPRequestContext(
        user_api_key_auth="auth-object",
        mcp_auth_header="legacy-header",
        mcp_server_auth_headers={"deepwiki": {"authorization": "Bearer per-server"}},
        oauth2_headers={"authorization": "Bearer oauth"},
        raw_headers={"x-trace": "abc"},
        request_tags=["team-a"],
        litellm_trace_id="trace-123",
        litellm_call_id="call-456",
    )

    process = AsyncMock(return_value=([], {}))
    execute = AsyncMock(return_value=[{"tool_call_id": "toolu_1", "result": "ok", "name": "t"}])
    responses = [
        {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
    ]

    with (
        patch.object(MCPRequestContext, "resolve", return_value=context),
        patch.object(
            mcp_handler.LiteLLM_Proxy_MCP_Handler
            if hasattr(mcp_handler, "LiteLLM_Proxy_MCP_Handler")
            else __import__(
                "litellm.responses.mcp.litellm_proxy_mcp_handler", fromlist=["LiteLLM_Proxy_MCP_Handler"]
            ).LiteLLM_Proxy_MCP_Handler,
            "_process_mcp_tools_without_openai_transform",
            new=process,
        ),
        patch(
            "litellm.responses.mcp.litellm_proxy_mcp_handler.LiteLLM_Proxy_MCP_Handler._execute_tool_calls",
            new=execute,
        ),
        patch("litellm.anthropic_messages", new=AsyncMock(side_effect=responses)),
    ):
        await mcp_handler.anthropic_messages_with_mcp(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-5",
            tools=[MCP_REFERENCE],
        )

    listing = process.call_args.kwargs
    assert listing["mcp_auth_header"] == "legacy-header", "tool listing must use the caller's MCP auth"
    assert listing["mcp_server_auth_headers"] == {"deepwiki": {"authorization": "Bearer per-server"}}
    assert listing["request_tags"] == ["team-a"]
    assert listing["litellm_trace_id"] == "trace-123"

    execution = execute.call_args.kwargs
    assert execution["user_api_key_auth"] == "auth-object"
    assert execution["mcp_auth_header"] == "legacy-header", "tool execution must use the caller's MCP auth"
    assert execution["mcp_server_auth_headers"] == {"deepwiki": {"authorization": "Bearer per-server"}}
    assert execution["oauth2_headers"] == {"authorization": "Bearer oauth"}
    assert execution["raw_headers"] == {"x-trace": "abc"}
    assert execution["litellm_call_id"] == "call-456"
    assert execution["litellm_trace_id"] == "trace-123"
    assert execution["request_tags"] == ["team-a"]


@pytest.mark.asyncio
async def test_anthropic_messages_with_mcp_stops_when_every_tool_call_is_skipped():
    """
    Regression test (LIT-4517): a tool_use turn whose calls all get skipped must
    end the loop, not send an empty tool_result message.

    Given: The model asks for a tool but the executor skips it (unresolvable name)
    When:  The gateway loop handles the empty result set
    Then:  It returns the last response instead of calling the model again

    _build_tool_result_message([]) produces a user message with empty content, and
    Anthropic rejects that, so the caller would get an unhandled 400 from the middle
    of the loop rather than the model's own answer.
    """
    from litellm.llms.anthropic.experimental_pass_through.messages import mcp_handler
    from litellm.responses.mcp.request_context import MCPRequestContext

    tool_use_response = {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "gone", "input": {}}],
    }
    anthropic_messages_mock = AsyncMock(return_value=tool_use_response)

    with (
        patch.object(MCPRequestContext, "resolve", return_value=MCPRequestContext(user_api_key_auth="auth")),
        patch(
            "litellm.responses.mcp.litellm_proxy_mcp_handler.LiteLLM_Proxy_MCP_Handler._process_mcp_tools_without_openai_transform",
            new=AsyncMock(return_value=([], {})),
        ),
        patch(
            "litellm.responses.mcp.litellm_proxy_mcp_handler.LiteLLM_Proxy_MCP_Handler._execute_tool_calls",
            new=AsyncMock(return_value=[]),
        ),
        patch("litellm.anthropic_messages", new=anthropic_messages_mock),
    ):
        result = await mcp_handler.anthropic_messages_with_mcp(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-5",
            tools=[MCP_REFERENCE],
        )

    assert anthropic_messages_mock.await_count == 1, (
        "With no tool results there is nothing to send back, so the loop must not call the model again"
    )
    assert result == tool_use_response
