"""SpendLog-backed session records for DeepSeek Anthropic Responses."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from litellm.llms.deepseek.anthropic_protocol import deepseek_anthropic_session_manifest
from litellm.responses.utils import ResponsesAPIRequestUtils


_SESSION_RECORD_FIELD = "_deepseek_anthropic_session"


@dataclass(frozen=True, slots=True)
class DeepSeekResponsesSession:
    response_id: str
    messages: tuple[dict[str, object], ...]
    suffix_manifest: dict[str, object] | None
    durability: str = "staged"

    @property
    def history_reasoning_required(self) -> bool:
        return self.suffix_manifest is not None

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "response_id": self.response_id,
            "messages": [deepcopy(message) for message in self.messages],
            "history_reasoning_required": self.history_reasoning_required,
            "suffix_manifest": deepcopy(self.suffix_manifest),
            "durability": self.durability,
        }

    def spend_log_marker(self) -> dict[str, object]:
        return {
            "version": 1,
            "response_id": self.response_id,
            "history_reasoning_required": self.history_reasoning_required,
            "suffix_manifest": deepcopy(self.suffix_manifest),
            "durability": "atomic",
        }


def create_deepseek_responses_session(
    response_id: str, messages: Sequence[Mapping[str, object]], *, durability: str = "staged"
) -> DeepSeekResponsesSession:
    canonical_messages = tuple(deepcopy(dict(message)) for message in messages)
    return DeepSeekResponsesSession(
        response_id=response_id,
        messages=canonical_messages,
        suffix_manifest=deepseek_anthropic_session_manifest(canonical_messages),
        durability=durability,
    )


SessionLoader = Callable[[str], Awaitable[DeepSeekResponsesSession | None]]
SessionCommitter = Callable[[DeepSeekResponsesSession], Awaitable[None]]


class SpendLogDeepSeekResponsesSessionRepository:
    def __init__(
        self,
        *,
        load_atomic_session: SessionLoader | None = None,
        commit_atomic_session: SessionCommitter | None = None,
    ):
        self._load_atomic_session = load_atomic_session
        self._commit_atomic_session = commit_atomic_session

    @property
    def requires_atomic_session(self) -> bool:
        return True

    @property
    def supports_atomic_session(self) -> bool:
        return self._load_atomic_session is not None and self._commit_atomic_session is not None

    async def load(self, previous_response_id: str) -> DeepSeekResponsesSession | None:
        if not self.supports_atomic_session or self._load_atomic_session is None:
            return None
        response_id = ResponsesAPIRequestUtils.decode_previous_response_id_to_original_previous_response_id(
            previous_response_id
        )
        if not response_id:
            return None
        return await self._load_atomic_session(response_id)

    async def commit(self, session: DeepSeekResponsesSession) -> None:
        if self._commit_atomic_session is None:
            return
        atomic_session = DeepSeekResponsesSession(
            response_id=session.response_id,
            messages=session.messages,
            suffix_manifest=session.suffix_manifest,
            durability="atomic",
        )
        await self._commit_atomic_session(atomic_session)

    @staticmethod
    def stage(
        proxy_server_request: object,
        response_id: str,
        messages: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        payload = create_deepseek_responses_session(response_id, messages).spend_log_marker()
        if not isinstance(proxy_server_request, dict):
            return payload
        proxy_request = cast(dict[str, object], proxy_server_request)
        body = proxy_request.get("body")
        if not isinstance(body, dict):
            return payload
        proxy_request["body"] = {
            **cast(dict[str, object], body),
            _SESSION_RECORD_FIELD: deepcopy(payload),
        }
        return payload


__all__ = [
    "DeepSeekResponsesSession",
    "SpendLogDeepSeekResponsesSessionRepository",
    "create_deepseek_responses_session",
]
