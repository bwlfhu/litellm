import copy
from typing import Final

import pytest

from litellm.llms.base_llm.responses.collaboration import PlaintextCollaboration

CLIENT_NAMESPACE: Final = "collaboration"
UPSTREAM_NAMESPACE: Final = "codex_collaboration_plaintext"
MESSAGE: Final = 'Keep all content, including unicode λ and "quotes".\nSecond line.'
ARGUMENTS: Final = '{"message":"hello","target":"child"}'


def _tools(namespace: str = CLIENT_NAMESPACE, encrypted: bool = True) -> list[dict[str, object]]:
    return [
        {
            "type": "namespace",
            "name": namespace,
            "description": "Agent communication",
            "tools": [
                {
                    "type": "function",
                    "name": action,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "Full message",
                                **({"encrypted": True} if encrypted else {}),
                            },
                            "target": {"type": "string", "encrypted": True},
                        },
                        "required": ["message"],
                    },
                }
                for action in ("spawn_agent", "send_message", "followup_task")
            ]
            + [{"type": "function", "name": "wait_agent", "parameters": {"type": "object"}}],
        }
    ]


def _call(namespace: str = UPSTREAM_NAMESPACE, name: str = "send_message") -> dict[str, object]:
    return {
        "type": "function_call",
        "id": "fc_call",
        "call_id": "call_1",
        "name": name,
        "namespace": namespace,
        "arguments": ARGUMENTS,
        "status": "completed",
        "provider_extension": {"keep": True},
    }


def test_request_projection_preserves_message_text_ciphertext_and_nonmessage_schema() -> None:
    adapter: Final = PlaintextCollaboration()
    history: Final = [
        {"role": "user", "content": MESSAGE},
        {**_call(CLIENT_NAMESPACE), "encrypted_function_args": [{"name": "message", "content": "old-ciphertext"}]},
        {"type": "function_call_output", "call_id": "call_1", "output": "unchanged result"},
        {"type": "agent_message", "content": [{"type": "input_text", "text": MESSAGE}]},
        {"type": "reasoning", "encrypted_content": "reasoning-ciphertext", "summary": []},
        {"type": "additional_tools", "tools": _tools()},
        {"type": "tool_search_output", "call_id": "search_1", "tools": _tools()},
    ]
    request: Final = {"model": "gpt-6-astra", "tools": _tools(), "input": history, "store": False}
    original: Final = copy.deepcopy(request)

    projected: Final = adapter.prepare_request(request)

    assert projected == {
        **request,
        "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False),
        "input": [
            history[0],
            {**history[1], "namespace": UPSTREAM_NAMESPACE},
            *history[2:5],
            {"type": "additional_tools", "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False)},
            {"type": "tool_search_output", "call_id": "search_1", "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False)},
        ],
    }
    assert request == original


@pytest.mark.parametrize("inventory_type", ["additional_tools", "tool_search_output"])
def test_historical_tool_inventory_can_activate_projection(inventory_type: str) -> None:
    adapter: Final = PlaintextCollaboration()
    request: Final = {"input": [{"type": inventory_type, "tools": _tools()}]}

    assert adapter.prepare_request(request) == {
        "input": [{"type": inventory_type, "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False)}],
        "store": False,
    }
    assert adapter.restore_response({"output": [_call()]}) == {
        "output": [{**_call(CLIENT_NAMESPACE), "encrypted_function_args": []}]
    }


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "namespace", "name": "collaboration", "tools": [{"type": "function", "name": "spawn_agent"}]},
        *_tools(encrypted=False),
        *_tools(namespace="ordinary_tools"),
        {
            "type": "namespace",
            "name": "collaboration",
            "tools": [
                {
                    "type": "function",
                    "name": "unrelated",
                    "parameters": {"properties": {"message": {"type": "string", "encrypted": True}}},
                }
            ],
        },
    ],
)
def test_unrelated_collaboration_schemas_and_unactivated_responses_are_identity_passthrough(
    tool: dict[str, object],
) -> None:
    adapter: Final = PlaintextCollaboration()
    request: Final = {"tools": [tool], "input": [{"role": "user", "content": MESSAGE}]}
    response: Final = {"output": [_call()]}
    event: Final = {"type": "response.output_item.added", "output_index": 0, "item": _call()}

    assert adapter.prepare_request(request) is request
    assert adapter.restore_response(response) is response
    assert adapter.restore_event(event) is event


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"type": "namespace", "name": CLIENT_NAMESPACE}, {"type": "namespace", "name": UPSTREAM_NAMESPACE}),
        (
            {"type": "function", "namespace": CLIENT_NAMESPACE, "name": "send_message"},
            {"type": "function", "namespace": UPSTREAM_NAMESPACE, "name": "send_message"},
        ),
        (
            {"type": "function", "name": "collaboration.spawn_agent"},
            {"type": "function", "name": "codex_collaboration_plaintext.spawn_agent"},
        ),
        (
            {"type": "function", "name": "collaboration__followup_task"},
            {"type": "function", "name": "codex_collaboration_plaintext__followup_task"},
        ),
    ],
)
def test_forced_and_allowed_tool_choices_follow_the_projected_inventory(
    choice: dict[str, object], expected: dict[str, object]
) -> None:
    adapter: Final = PlaintextCollaboration()
    request: Final = {"tools": _tools(), "tool_choice": choice}
    allowed: Final = {
        "type": "allowed_tools",
        "mode": "required",
        "tools": [choice, {"type": "function", "name": "other"}],
    }

    assert adapter.prepare_request(request) == {
        "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False),
        "tool_choice": expected,
        "store": False,
    }
    assert adapter.prepare_request({"tools": _tools(), "tool_choice": allowed}) == {
        "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False),
        "tool_choice": {**allowed, "tools": [expected, {"type": "function", "name": "other"}]},
        "store": False,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"store": True},
        {"previous_response_id": "resp_previous"},
        {"conversation": "conv_previous"},
        {"input": [{"type": "additional_tools", "tools": _tools(UPSTREAM_NAMESPACE)}]},
    ],
)
def test_active_projection_rejects_stateful_requests_and_alias_collisions(extra: dict[str, object]) -> None:
    adapter: Final = PlaintextCollaboration()

    with pytest.raises(ValueError, match=r"stateless Responses|reserved by the Responses adapter"):
        adapter.prepare_request({"tools": _tools(), **extra})


@pytest.mark.parametrize("separator", [".", "__"])
def test_qualified_history_and_returned_names_keep_call_identifiers(separator: str) -> None:
    adapter: Final = PlaintextCollaboration()
    history: Final = {
        "type": "function_call",
        "id": "fc_previous",
        "call_id": "call_previous",
        "name": CLIENT_NAMESPACE + separator + "spawn_agent",
        "arguments": ARGUMENTS,
    }

    assert adapter.prepare_request({"tools": _tools(), "input": [history]}) == {
        "tools": _tools(UPSTREAM_NAMESPACE, encrypted=False),
        "input": [{**history, "namespace": UPSTREAM_NAMESPACE, "name": "spawn_agent"}],
        "store": False,
    }
    assert adapter.restore_response(
        {"output": [{**history, "name": UPSTREAM_NAMESPACE + separator + "spawn_agent"}]}
    ) == {"output": [{**history, "namespace": CLIENT_NAMESPACE, "name": "spawn_agent", "encrypted_function_args": []}]}


@pytest.mark.parametrize(
    "item_type,field",
    [("agent_message", "content"), ("function_call_output", "output"), ("custom_tool_call_output", "output")],
)
def test_existing_ciphertext_disables_plaintext_projection(item_type: str, field: str) -> None:
    adapter: Final = PlaintextCollaboration()
    request: Final = {
        "tools": _tools(),
        "input": [{"type": item_type, field: [{"type": "encrypted_content", "encrypted_content": "opaque-history"}]}],
    }
    assert adapter.prepare_request(request) is request
    assert not adapter.active
    response: Final = {"output": [_call(CLIENT_NAMESPACE)]}
    assert adapter.restore_response(response) is response


@pytest.mark.parametrize("marker", [None, ["message"]])
def test_plaintext_response_rejects_an_explicit_encrypted_marker(marker: object) -> None:
    adapter: Final = PlaintextCollaboration()
    adapter.prepare_request({"tools": _tools()})
    with pytest.raises(ValueError, match="encrypted arguments"):
        adapter.restore_response({"output": [{**_call(), "encrypted_function_args": marker}]})


def test_stream_state_preserves_plaintext_marker_in_sparse_and_terminal_events() -> None:
    adapter: Final = PlaintextCollaboration()
    adapter.prepare_request({"tools": _tools()})
    call: Final = _call()
    adapter.restore_event({"type": "response.output_item.added", "output_index": 0, "item": call})
    event: Final = adapter.restore_event(
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": ARGUMENTS}
    )
    assert event == {
        "type": "response.function_call_arguments.done",
        "output_index": 0,
        "arguments": ARGUMENTS,
        "name": "send_message",
        "namespace": CLIENT_NAMESPACE,
        "call_id": call["call_id"],
        "encrypted_function_args": [],
    }
    terminal: Final = adapter.restore_event({"type": "response.completed", "response": {"output": [call]}})
    assert terminal == {
        "type": "response.completed",
        "response": {"output": [{**call, "namespace": CLIENT_NAMESPACE, "encrypted_function_args": []}]},
    }
