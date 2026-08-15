"""SpendLog-backed session records for DeepSeek Anthropic Responses."""

from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from json import JSONDecodeError, dumps
from typing import cast

from cryptography.exceptions import InvalidTag
from litellm.litellm_core_utils.app_crypto import AppCrypto
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

    def is_valid_atomic_session(self, response_id: str) -> bool:
        return (
            self.response_id == response_id
            and self.durability == "atomic"
            and self.suffix_manifest == deepseek_anthropic_session_manifest(self.messages)
        )


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
SessionEncryptionKeyLoader = Callable[[], str | None]
SessionJsonEncoder = Callable[[object], object]


def _proxy_session_encryption_key() -> str | None:
    from litellm.proxy.proxy_server import master_key

    return master_key if isinstance(master_key, str) and master_key else None


def _encode_prisma_json(value: object) -> object:
    if not isinstance(value, Mapping):
        raise ValueError("DeepSeek Responses session payload must be a JSON object")
    from prisma import Json  # noqa: PLC0415  # generated Prisma client is unavailable in lightweight tooling

    return Json(dict(value))


def _session_from_payload(payload: object) -> DeepSeekResponsesSession | None:
    if not isinstance(payload, Mapping):
        return None
    response_id = payload.get("response_id")
    messages = payload.get("messages")
    suffix_manifest = payload.get("suffix_manifest")
    durability = payload.get("durability")
    if not isinstance(response_id, str) or not isinstance(messages, list) or not isinstance(durability, str):
        return None
    if suffix_manifest is not None and not isinstance(suffix_manifest, Mapping):
        return None
    if not all(isinstance(message, Mapping) for message in messages):
        return None
    return DeepSeekResponsesSession(
        response_id=response_id,
        messages=tuple(deepcopy(dict(message)) for message in messages),
        suffix_manifest=deepcopy(dict(suffix_manifest)) if isinstance(suffix_manifest, Mapping) else None,
        durability=durability,
    )


def _record_field(record: object, field: str) -> object:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _session_aad(owner_id: str, response_id: str) -> bytes:
    return dumps((owner_id, response_id), ensure_ascii=True, separators=(",", ":")).encode()


class ProxyDeepSeekResponsesSessionRepository:
    def __init__(
        self,
        *,
        prisma_client: object,
        owner_id: str,
        encryption_key_loader: SessionEncryptionKeyLoader = _proxy_session_encryption_key,
        json_encoder: SessionJsonEncoder = _encode_prisma_json,
    ):
        self._prisma_client = prisma_client
        self._owner_id = owner_id
        self._encryption_key_loader = encryption_key_loader
        self._json_encoder = json_encoder

    @property
    def requires_atomic_session(self) -> bool:
        return True

    @property
    def supports_atomic_session(self) -> bool:
        return self._prisma_client is not None and bool(self._owner_id) and self._encryption_key_loader() is not None

    def _crypto(self) -> AppCrypto:
        encryption_key = self._encryption_key_loader()
        if not isinstance(encryption_key, str) or not encryption_key:
            raise RuntimeError("DeepSeek Responses session encryption is unavailable")
        return AppCrypto(sha256(encryption_key.encode()).digest())

    @property
    def _table(self) -> object:
        database = getattr(self._prisma_client, "db", None)
        table = getattr(database, "litellm_deepseekresponsessession", None)
        if table is None:
            raise RuntimeError("DeepSeek Responses session storage is unavailable")
        return table

    async def load(self, previous_response_id: str) -> DeepSeekResponsesSession | None:
        if not self.supports_atomic_session:
            return None
        response_id = ResponsesAPIRequestUtils.decode_previous_response_id_to_original_previous_response_id(
            previous_response_id
        )
        if not response_id:
            return None
        find_unique = getattr(self._table, "find_unique", None)
        if not callable(find_unique):
            raise RuntimeError("DeepSeek Responses session storage is unavailable")
        record = await find_unique(where={"response_id": response_id})
        if record is None or _record_field(record, "owner_id") != self._owner_id:
            return None
        encrypted_payload = _record_field(record, "encrypted_payload")
        try:
            payload = self._crypto().decrypt_json(
                dict(encrypted_payload) if isinstance(encrypted_payload, Mapping) else {},
                aad=_session_aad(self._owner_id, response_id),
            )
        except (BinasciiError, InvalidTag, JSONDecodeError, KeyError, TypeError, UnicodeDecodeError, ValueError):
            return None
        session = _session_from_payload(payload)
        return session if session is not None and session.is_valid_atomic_session(response_id) else None

    async def commit(self, session: DeepSeekResponsesSession) -> None:
        if not self.supports_atomic_session:
            raise RuntimeError("DeepSeek Responses session storage is unavailable")
        upsert = getattr(self._table, "upsert", None)
        if not callable(upsert):
            raise RuntimeError("DeepSeek Responses session storage is unavailable")
        encrypted_payload = self._crypto().encrypt_json(
            session.payload(), aad=_session_aad(self._owner_id, session.response_id)
        )
        encrypted_json = self._json_encoder(encrypted_payload)
        await upsert(
            where={"response_id": session.response_id},
            data={
                "create": {
                    "response_id": session.response_id,
                    "owner_id": self._owner_id,
                    "encrypted_payload": encrypted_json,
                },
                "update": {
                    "owner_id": self._owner_id,
                    "encrypted_payload": encrypted_json,
                },
            },
        )


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
        session = await self._load_atomic_session(response_id)
        return session if session is not None and session.is_valid_atomic_session(response_id) else None

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
    "ProxyDeepSeekResponsesSessionRepository",
    "SpendLogDeepSeekResponsesSessionRepository",
    "create_deepseek_responses_session",
]
