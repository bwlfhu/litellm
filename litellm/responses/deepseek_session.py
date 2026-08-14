"""SpendLog-backed session records for DeepSeek Anthropic Responses."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from litellm.llms.deepseek.anthropic_protocol import deepseek_anthropic_session_manifest
from litellm.responses.litellm_completion_transformation.session_handler import ResponsesSessionHandler
from litellm.responses.utils import ResponsesAPIRequestUtils


_SESSION_RECORD_FIELD = "_deepseek_anthropic_session"


@dataclass(frozen=True, slots=True)
class DeepSeekResponsesSession:
    response_id: str
    messages: tuple[dict[str, object], ...]
    suffix_manifest: dict[str, object] | None

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
        }


def create_deepseek_responses_session(
    response_id: str, messages: Sequence[Mapping[str, object]]
) -> DeepSeekResponsesSession:
    canonical_messages = tuple(deepcopy(dict(message)) for message in messages)
    return DeepSeekResponsesSession(
        response_id=response_id,
        messages=canonical_messages,
        suffix_manifest=deepseek_anthropic_session_manifest(canonical_messages),
    )


def _session_from_payload(payload: object, expected_response_id: str) -> DeepSeekResponsesSession | None:
    if not isinstance(payload, Mapping) or payload.get("version") != 1 or payload.get("response_id") != expected_response_id:
        return None
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not all(isinstance(message, Mapping) for message in raw_messages):
        return None
    messages = tuple(deepcopy(dict(message)) for message in raw_messages)
    expected_manifest = deepseek_anthropic_session_manifest(messages)
    stored_manifest = payload.get("suffix_manifest")
    history_reasoning_required = payload.get("history_reasoning_required")
    if history_reasoning_required is not (expected_manifest is not None):
        return None
    if expected_manifest is None:
        if stored_manifest is not None:
            return None
    elif not isinstance(stored_manifest, Mapping) or dict(stored_manifest) != expected_manifest:
        return None
    return DeepSeekResponsesSession(
        response_id=expected_response_id,
        messages=messages,
        suffix_manifest=deepcopy_manifest(expected_manifest),
    )


def deepcopy_manifest(manifest: dict[str, object] | None) -> dict[str, object] | None:
    return deepcopy(manifest)


SpendLogLoader = Callable[[str], Awaitable[Sequence[Mapping[str, object]]]]
ProxyRequestLoader = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object] | None]]


class SpendLogDeepSeekResponsesSessionRepository:
    def __init__(
        self,
        load_spend_logs: SpendLogLoader = ResponsesSessionHandler.get_all_spend_logs_for_previous_response_id,
        load_proxy_request: ProxyRequestLoader = ResponsesSessionHandler.get_proxy_server_request_from_spend_log,
    ):
        self._load_spend_logs = load_spend_logs
        self._load_proxy_request = load_proxy_request

    async def load(self, previous_response_id: str) -> DeepSeekResponsesSession | None:
        decoded_response_id = ResponsesAPIRequestUtils._decode_responses_api_response_id(previous_response_id)
        response_id = decoded_response_id.get("response_id", previous_response_id)
        if not isinstance(response_id, str) or not response_id:
            return None
        spend_logs = await self._load_spend_logs(previous_response_id)
        matching_logs = tuple(log for log in spend_logs if log.get("request_id") == response_id)
        if len(matching_logs) != 1:
            return None
        proxy_request = await self._load_proxy_request(matching_logs[0])
        if not isinstance(proxy_request, Mapping):
            return None
        return _session_from_payload(proxy_request.get(_SESSION_RECORD_FIELD), response_id)

    @staticmethod
    def stage(
        proxy_server_request: object,
        response_id: str,
        messages: Sequence[Mapping[str, object]],
    ) -> None:
        if not isinstance(proxy_server_request, dict):
            return
        body = proxy_server_request.get("body")
        if not isinstance(body, dict):
            return
        body[_SESSION_RECORD_FIELD] = create_deepseek_responses_session(response_id, messages).payload()


__all__ = [
    "DeepSeekResponsesSession",
    "SpendLogDeepSeekResponsesSessionRepository",
    "create_deepseek_responses_session",
]
