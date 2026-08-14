from litellm.router_protocol import (
    DeploymentProtocolContext,
    DeploymentReasoningProtocol,
    build_deployment_protocol_context,
    protocol_context_from_kwargs,
    resolve_deployment_protocol,
    sanitize_model_info,
)


def test_router_protocol_context_requires_router_provenance_and_matches_attempt():
    context = build_deployment_protocol_context(
        {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic"},
        "deployment-a",
        "attempt-a",
    )

    assert context is not None
    assert resolve_deployment_protocol(context, deployment_id="deployment-a", attempt_id="attempt-a") == (
        DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC
    )
    assert resolve_deployment_protocol(context, deployment_id="deployment-b", attempt_id="attempt-a") is None
    assert resolve_deployment_protocol(context, deployment_id="deployment-a", attempt_id="attempt-b") is None
    assert resolve_deployment_protocol(
        DeploymentProtocolContext(
            protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
            deployment_id="deployment-a",
            attempt_id="attempt-a",
            _provenance=object(),
        )
    ) is None


def test_router_protocol_context_does_not_trust_public_kwargs_or_leak_model_info():
    trusted_context = build_deployment_protocol_context(
        {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic", "tier": "paid"},
        "deployment-a",
        "attempt-a",
    )

    assert trusted_context is not None
    assert sanitize_model_info({"reasoning_protocol": "deepseek_anthropic", "tier": "paid"}) == {"tier": "paid"}
    assert protocol_context_from_kwargs({"model_info": {"reasoning_protocol": "deepseek_anthropic"}}) is None
    assert protocol_context_from_kwargs(
        {"_litellm_deployment_protocol_context": trusted_context},
        deployment_id="deployment-a",
        attempt_id="attempt-a",
    ) == trusted_context
