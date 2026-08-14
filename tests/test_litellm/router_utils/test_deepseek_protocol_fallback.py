import pytest

from litellm.llms.deepseek.anthropic_protocol import DeepSeekProtocolError, DeepSeekProtocolNonFallbackError
from litellm.router import Router


async def _raise_protocol(*args, **kwargs):
    raise DeepSeekProtocolError("reasoning_history_missing")


@pytest.mark.asyncio
async def test_deepseek_protocol_error_blocks_async_same_group_and_cross_group_fallback():
    router = Router(model_list=[])
    calls = 0

    async def original(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise DeepSeekProtocolError("reasoning_history_missing")

    with pytest.raises(DeepSeekProtocolNonFallbackError) as raised:
        await router.async_function_with_fallbacks(
            original_function=original,
            model="primary",
            fallbacks=["backup"],
            num_retries=2,
        )

    assert raised.value.code == "reasoning_history_missing"
    assert raised.value.fallback_allowed is False
    assert calls == 1


def test_deepseek_protocol_error_blocks_sync_fallback():
    router = Router(model_list=[])

    with pytest.raises(DeepSeekProtocolNonFallbackError):
        router.function_with_fallbacks(
            original_function=_raise_protocol,
            model="primary",
            fallbacks=["backup"],
            num_retries=1,
        )
