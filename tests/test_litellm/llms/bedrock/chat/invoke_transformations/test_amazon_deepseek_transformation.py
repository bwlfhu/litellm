import pytest

from litellm.llms.bedrock.chat.invoke_transformations.amazon_deepseek_transformation import (
    AmazonDeepseekR1ResponseIterator,
)

REASONING = "Let me think about this."
ANSWER = "The answer is 4."


def _drain(generations: list[str]) -> tuple[str, str]:
    iterator = AmazonDeepseekR1ResponseIterator(streaming_response=None, sync_stream=True)
    deltas = tuple(
        iterator.chunk_parser(
            {
                "generation": generation,
                "stop_reason": "stop" if position == len(generations) - 1 else None,
                "prompt_token_count": 1,
                "generation_token_count": 1,
            }
        )
        .choices[0]
        .delta
        for position, generation in enumerate(generations)
    )
    return (
        "".join(getattr(delta, "reasoning_content", None) or "" for delta in deltas),
        "".join(getattr(delta, "content", None) or "" for delta in deltas),
    )


@pytest.mark.parametrize(
    "generations",
    [
        pytest.param([REASONING, "</think>", ANSWER], id="marker_alone"),
        pytest.param([REASONING, "</", "think>", ANSWER], id="marker_split_in_two"),
        pytest.param([REASONING, *"</think>", ANSWER], id="marker_split_per_character"),
        pytest.param([REASONING, f"</think>{ANSWER}"], id="marker_glued_to_answer"),
        pytest.param([f"{REASONING}</think>", ANSWER], id="marker_glued_to_reasoning"),
        pytest.param([f"{REASONING}</think>{ANSWER}"], id="whole_turn_in_one_chunk"),
    ],
)
def test_end_of_thinking_is_found_however_marker_is_chunked(generations):
    assert _drain(generations) == (REASONING, ANSWER)


def test_reasoning_and_content_do_not_depend_on_chunk_size():
    whole = f"{REASONING}</think>{ANSWER}"
    results = {
        _drain([whole[index : index + size] for index in range(0, len(whole), size)])
        for size in (1, 2, 3, 5, 8, 13, len(whole))
    }

    assert results == {(REASONING, ANSWER)}


def test_unterminated_marker_prefix_is_released_at_stream_end():
    assert _drain(["still thinking ", "</thi"]) == ("still thinking </thi", "")


def test_marker_in_answer_is_content_after_thinking_ended():
    assert _drain([REASONING, "</think>", "write </think> literally"]) == (
        REASONING,
        "write </think> literally",
    )


def test_usage_and_finish_reason_are_preserved():
    chunk = AmazonDeepseekR1ResponseIterator(streaming_response=None, sync_stream=True).chunk_parser(
        {
            "generation": "done",
            "stop_reason": "stop",
            "prompt_token_count": 11,
            "generation_token_count": 7,
        }
    )

    assert chunk.choices[0].finish_reason == "stop"
    assert chunk.usage["prompt_tokens"] == 11
    assert chunk.usage["completion_tokens"] == 7
    assert chunk.usage["total_tokens"] == 18
