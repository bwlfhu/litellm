import json
from collections.abc import Mapping, Sequence
from itertools import chain
from typing import Final, Literal

from pydantic import TypeAdapter

_MAPPING: Final = TypeAdapter(Mapping[str, object])
_TAGS: Final = (("<thinking>", "</thinking>"), ("<think>", "</think>"))


def _opening_tag(text: str) -> tuple[str, str] | None:
    stripped: Final = text.lstrip().lower()
    return next((pair for pair in _TAGS if stripped.startswith(pair[0])), None)


def normalize_inline_thinking_blocks(content: Sequence[object]) -> tuple[object, ...]:
    return tuple(chain.from_iterable(_normalized_block(block) for block in content))


def _normalized_block(value: object) -> tuple[object, ...]:
    if not isinstance(value, Mapping):
        return (value,)
    block: Final = _MAPPING.validate_python(value)
    text: Final = block.get("text")
    if block.get("type") != "text" or not isinstance(text, str):
        return (block,)
    opening: Final = _opening_tag(text)
    if opening is None:
        return (block,)
    body: Final = text.lstrip()[len(opening[0]) :]
    end: Final = body.lower().find(opening[1])
    reasoning: Final = body if end < 0 else body[:end]
    remainder: Final = "" if end < 0 else body[end + len(opening[1]) :]
    return (
        *(({"type": "thinking", "thinking": reasoning},) if reasoning else ()),
        *(({**block, "text": remainder},) if remainder else ()),
    )


class InlineThinkingStream:
    def __init__(self) -> None:
        self._offset = 0
        self._input_index: int | None = None
        self._output_index = 0
        self._template: Mapping[str, object] = {}
        self._mode: Literal["prefix", "thinking", "text"] = "prefix"
        self._pending = ""
        self._closing = ""
        self._open = False
        self._emitted = 0

    def event(self, event: bytes) -> tuple[bytes, ...]:
        lines: Final = event.splitlines()
        data: Final = tuple(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
        if not data:
            return (event,)
        try:
            payload: Final = _MAPPING.validate_json(b"\n".join(data))
        except ValueError:
            return (event,)
        headers: Final = tuple(line for line in lines if line and not line.startswith((b"data:", b"event:")))
        return tuple(self._encode(item, headers if i == 0 else ()) for i, item in enumerate(self._transform(payload)))

    @staticmethod
    def _encode(payload: Mapping[str, object], headers: Sequence[bytes]) -> bytes:
        return (
            b"\n".join(
                (
                    *headers,
                    f"event: {payload.get('type', 'message')}".encode(),
                    b"data: " + json.dumps(payload).encode(),
                )
            )
            + b"\n\n"
        )

    def _transform(self, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        event_type: Final = payload.get("type")
        index: Final = payload.get("index")
        raw_block: Final = payload.get("content_block")
        block: Final = _MAPPING.validate_python(raw_block) if isinstance(raw_block, Mapping) else None
        if event_type == "content_block_start" and block is not None and block.get("type") == "text":
            if not isinstance(index, int):
                return (payload,)
            self._input_index = index
            self._template = payload
            self._mode = "prefix"
            self._pending = ""
            self._open = False
            self._emitted = 0
            initial: Final = block.get("text")
            return self._feed(initial if isinstance(initial, str) else "")
        if index == self._input_index and self._input_index is not None:
            if event_type == "content_block_delta":
                raw_delta: Final = payload.get("delta")
                delta: Final = _MAPPING.validate_python(raw_delta) if isinstance(raw_delta, Mapping) else None
                if delta is not None and delta.get("type") == "text_delta":
                    text: Final = delta.get("text")
                    return self._feed(text if isinstance(text, str) else "")
                prefix: Final = self._feed("", final=True)
                return (*prefix, {**payload, "index": self._output_index})
            if event_type == "content_block_stop":
                final: Final = self._feed("", final=True)
                empty: Final = self._start("text") if not self._emitted else ()
                stop: Final = self._close()
                self._input_index = None
                return (*final, *empty, *stop)
        return ({**payload, "index": index + self._offset},) if isinstance(index, int) else (payload,)

    def _start(self, kind: Literal["thinking", "text"]) -> tuple[Mapping[str, object], ...]:
        if self._open:
            return ()
        if self._emitted:
            self._offset += 1
        self._output_index = (self._input_index or 0) + self._offset
        self._emitted += 1
        self._open = True
        original: Final = self._template.get("content_block")
        fields: Final[Mapping[str, object]] = (
            _MAPPING.validate_python(original) if isinstance(original, Mapping) else {}
        )
        block: Final = {"type": "thinking", "thinking": ""} if kind == "thinking" else {**fields, "text": ""}
        return ({**self._template, "index": self._output_index, "content_block": block},)

    def _delta(self, text: str, kind: Literal["thinking", "text"]) -> tuple[Mapping[str, object], ...]:
        if not text:
            return ()
        start: Final = self._start(kind)
        return (
            *start,
            {
                "type": "content_block_delta",
                "index": self._output_index,
                "delta": {"type": kind + "_delta", kind: text},
            },
        )

    def _close(self) -> tuple[Mapping[str, object], ...]:
        if not self._open:
            return ()
        self._open = False
        return ({"type": "content_block_stop", "index": self._output_index},)

    def _feed(self, text: str, final: bool = False) -> tuple[Mapping[str, object], ...]:
        self._pending += text
        if self._mode == "prefix":
            opening: Final = _opening_tag(self._pending)
            if opening is not None:
                self._mode = "thinking"
                self._closing = opening[1]
                self._pending = self._pending.lstrip()[len(opening[0]) :]
            elif (
                not final
                and len(self._pending) < 64
                and any(tag.startswith(self._pending.lstrip().lower()) for tag, _ in _TAGS)
            ):
                return ()
            else:
                self._mode = "text"
        if self._mode == "text":
            pending_text: Final = self._pending
            self._pending = ""
            return self._delta(pending_text, "text")
        closing: Final = self._pending.lower().find(self._closing)
        if closing >= 0:
            reasoning: Final = self._delta(self._pending[:closing], "thinking")
            stop: Final = self._close()
            self._pending = self._pending[closing + len(self._closing) :]
            self._mode = "text"
            return (*reasoning, *stop, *self._feed("", final=final))
        retained: Final = 0 if final else len(self._closing) - 1
        emitted: Final = self._pending if retained == 0 else self._pending[:-retained]
        self._pending = "" if retained == 0 else self._pending[-retained:]
        return self._delta(emitted, "thinking")
