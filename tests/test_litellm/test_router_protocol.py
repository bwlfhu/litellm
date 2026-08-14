from litellm.router_protocol import (
    DeploymentProtocolContext,
    DeploymentRateSnapshot,
    DeploymentReasoningProtocol,
    _ROUTER_PROVENANCE,
    _activate_router_protocol_context,
    _build_deployment_protocol_context,
    protocol_context_from_kwargs,
    resolve_deployment_protocol,
    sanitize_model_info,
)


def test_router_protocol_context_requires_router_provenance_and_matches_attempt():
    assert (
        _build_deployment_protocol_context(
            {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic", "max_input_tokens": 4096},
            "deployment-a",
            "attempt-a",
        )
        is None
    )
    assert (
        resolve_deployment_protocol(
            DeploymentProtocolContext(
                protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
                deployment_id="deployment-a",
                attempt_id="attempt-a",
                suffix_token_budget=4096,
                rate_snapshot=DeploymentRateSnapshot(),
                _provenance=object(),
            )
        )
        is None
    )


def test_router_protocol_context_does_not_trust_public_kwargs_or_leak_model_info():
    assert sanitize_model_info(
        {
            "reasoning_protocol": "deepseek_anthropic",
            "deepseek_reasoning_suffix_token_budget": 512,
            "tier": "paid",
        }
    ) == {"tier": "paid"}
    assert protocol_context_from_kwargs({"model_info": {"reasoning_protocol": "deepseek_anthropic"}}) is None


def test_direct_sdk_cannot_activate_a_router_provenanced_context():
    forged_context = DeploymentProtocolContext(
        protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
        deployment_id="deployment-a",
        attempt_id="attempt-a",
        suffix_token_budget=512,
        rate_snapshot=DeploymentRateSnapshot(),
        _provenance=_ROUTER_PROVENANCE,
    )

    with _activate_router_protocol_context(forged_context):
        assert protocol_context_from_kwargs({"_litellm_deployment_protocol_context": forged_context}) is None


def test_direct_sdk_private_factory_cannot_build_the_protocol():
    context = _build_deployment_protocol_context(
        {"id": "deployment-a", "reasoning_protocol": "deepseek_anthropic"},
        "deployment-a",
        "attempt-a",
    )

    assert context is None
