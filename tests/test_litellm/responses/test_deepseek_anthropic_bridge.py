import json

import httpx
import pytest

import litellm
from litellm.litellm_core_utils.asyncify import run_async_function
from litellm.llms.deepseek.anthropic_protocol import DeepSeekProtocolError
from litellm.router_protocol import build_deployment_protocol_context
from litellm.responses.deepseek_anthropic import DeepSeekAnthropicResponsesBridge, DeepSeekResponsesSessionStore


def _context():
    context = build_deployment_protocol_context(
        {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic"},
        "deployment-a",
        "attempt-a",
    )
    assert context is not None
    return context


def _unexpected_public_entrypoint(**kwargs):
    raise AssertionError("completion entrypoint must not be used")


@pytest.mark.asyncio
async def test_deepseek_responses_async_bridge_sends_one_anthropic_wire_request_without_completion(monkeypatch):
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_ds_1",
                "content": [{"type": "thinking", "thinking": "reason"}, {"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 4, "output_tokens": 3},
                "stop_reason": "end_turn",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(litellm, "completion", _unexpected_public_entrypoint)
    monkeypatch.setattr(litellm, "acompletion", _unexpected_public_entrypoint)
    response = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32, "reasoning": {"effort": "high"}},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    await client.aclose()

    assert len(requests) == 1
    assert requests[0]["messages"] == [{"role": "user", "content": "question"}]
    assert requests[0]["thinking"] == {"type": "enabled"}
    assert getattr(response.output[0], "type", None) == "reasoning"
    assert getattr(response.output[1], "type", None) == "message"


def test_deepseek_responses_sync_bridge_uses_same_raw_reconstruction_core():
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={"id": "resp_ds_sync", "content": [{"type": "text", "text": "answer"}], "usage": {}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="question",
        responses_api_request={"max_output_tokens": 32},
        custom_llm_provider="anthropic",
        _is_async=False,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    run_async_function(client.aclose)

    assert len(requests) == 1
    assert response.id == "resp_ds_sync"
    assert requests[0]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_deepseek_responses_bridge_preserves_reasoning_function_call_and_output_history():
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_ds_tool",
                "content": [
                    {"type": "thinking", "thinking": "call tool"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "x"}},
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3},
                "stop_reason": "tool_use",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input="find",
        responses_api_request={
            "max_output_tokens": 32,
            "reasoning": {"effort": "high"},
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        },
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    second = await DeepSeekAnthropicResponsesBridge.response_api_handler(
        model="deepseek-v4-pro",
        input=[{"type": "function_call_output", "call_id": "call-1", "output": "value"}],
        responses_api_request={"max_output_tokens": 32, "previous_response_id": first.id, "reasoning": {"effort": "high"}},
        custom_llm_provider="anthropic",
        _is_async=True,
        stream=False,
        protocol_context=_context(),
        client=client,
    )
    await client.aclose()

    assert len(requests) == 2
    assert requests[1]["messages"][1]["content"][0] == {"type": "thinking", "thinking": "call tool"}
    assert requests[1]["messages"][2]["content"][0]["tool_use_id"] == "call-1"
    assert second.previous_response_id == first.id


@pytest.mark.asyncio
async def test_deepseek_responses_effort_none_rejects_complete_tool_history_without_http():
    DeepSeekResponsesSessionStore.save(
        "resp_existing",
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}]},
        ],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request, json={})))
    with pytest.raises(DeepSeekProtocolError, match="reasoning_mode_conflict"):
        await DeepSeekAnthropicResponsesBridge.response_api_handler(
            model="deepseek-v4-pro",
            input="next",
            responses_api_request={"previous_response_id": "resp_existing", "reasoning": {"effort": "none"}},
            custom_llm_provider="anthropic",
            _is_async=True,
            stream=False,
            protocol_context=_context(),
            client=client,
        )
    await client.aclose()
