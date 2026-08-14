from litellm.responses.deepseek_accounting import (
    AttemptRateSnapshot,
    ParentAccounting,
    build_attempt_snapshot,
)


def test_deepseek_parent_accounting_aggregates_attempt_rates_and_cache_alias_once():
    primary = build_attempt_snapshot(
        model="deepseek-primary",
        deployment_id="deployment-primary",
        usage={"input_tokens": 100, "output_tokens": 20, "prompt_cache_hit_tokens": 40},
        rates=AttemptRateSnapshot(
            input_cost_per_token=1.0,
            output_cost_per_token=2.0,
            cache_read_input_cost_per_token=0.25,
        ),
    )
    backup = build_attempt_snapshot(
        model="deepseek-backup",
        deployment_id="deployment-backup",
        usage={"input_tokens": 80, "output_tokens": 10, "cache_read_input_tokens": 20},
        rates=AttemptRateSnapshot(
            input_cost_per_token=3.0,
            output_cost_per_token=4.0,
            cache_read_input_cost_per_token=0.5,
        ),
    )
    parent = ParentAccounting().add_attempt(primary).add_attempt(backup)

    assert primary.cost == 60 + 40 + 40 * 0.25
    assert backup.cost == 60 * 3 + 40 + 20 * 0.5
    assert parent.usage.input_tokens == 180
    assert parent.usage.output_tokens == 30
    assert parent.usage.cache_read_input_tokens == 60
    assert parent.cost == primary.cost + backup.cost
    summary = parent.spend_log_summary()
    assert summary["attempt_count"] == 2
    assert summary["cache_read_input_tokens"] == 60
    assert "reasoning_protocol" not in summary


def test_deepseek_parent_accounting_does_not_invent_cache_creation_tokens():
    snapshot = build_attempt_snapshot(
        model="deepseek",
        deployment_id="deployment",
        usage={"input_tokens": 12, "prompt_cache_hit_tokens": 3},
        rates=AttemptRateSnapshot(input_cost_per_token=1.0, cache_read_input_cost_per_token=0.5),
    )

    assert snapshot.usage.cache_read_input_tokens == 3
    assert snapshot.usage.cache_creation_input_tokens == 0
    assert snapshot.cost == 9 + 1.5
