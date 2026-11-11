import json
import re
from collections.abc import Iterator, Mapping, Sequence
from itertools import chain, groupby
from typing import Final, Literal, TypeAlias

from pydantic import TypeAdapter

_MAPPING: Final = TypeAdapter(Mapping[str, object])
_OPEN: Final = re.compile(r"<(?:thinking|think)>", re.IGNORECASE | re.ASCII)
_BASE: Final = re.compile(r"<(?:thinking|think)", re.IGNORECASE | re.ASCII)
_TOKEN: Final = re.compile(r"</?(?:thinking|think)>", re.IGNORECASE | re.ASCII)
_NAME: Final = re.compile(r"[A-Za-z0-9_:-]*")
_SPACE: Final = re.compile(r"\s*")
_OPENINGS: Final = ("<thinking>", "<think>")
_PROBE_SIZE: Final = 64
_TAIL_SIZE: Final = len("</thinking>") - 1
_Kind: TypeAlias = Literal["text", "thinking"]


def _opening(text: str, final: bool) -> tuple[Literal["wait", "text", "thinking"], int]:
    complete: Final = _OPEN.match(text)
    if complete is not None:
        return "thinking", complete.end()
    if not final and any(tag.startswith(text.lower()) for tag in _OPENINGS):
        return "wait", 0
    base: Final = _BASE.match(text)
    if base is None:
        return "text", 0
    suffix: Final = text[base.end() :]
    name: Final = _NAME.match(suffix)
    size: Final = name.end() if name is not None else 0
    if size and suffix[size:].startswith((">", "/>")):
        return "text", 0
    if size == len(suffix) and not final and len(text) < _PROBE_SIZE:
        return "wait", 0
    return "thinking", base.end()


class _ThinkingText:
    def __init__(self) -> None:
        self._mode: Literal["prefix", "thinking", "text"] = "prefix"
        self._depth = 0
        self._tail = ""
        self._data = ""
        self._cursor: int = 0

    def feed(self, text: str, final: bool = False) -> Iterator[tuple[_Kind, str]]:
        self._data = self._tail + text
        self._tail = ""
        self._cursor = 0
        while self._cursor < len(self._data):
            if self._mode == "text":
                yield "text", self._data[self._cursor :]
                break
            if self._mode == "prefix":
                yield from self._prefix(final)
            else:
                yield from self._thinking(final)
        self._data = ""

    def _prefix(self, final: bool) -> Iterator[tuple[_Kind, str]]:
        whitespace: Final = _SPACE.match(self._data, self._cursor)
        if whitespace is not None and whitespace.end() > self._cursor:
            yield "text", self._data[self._cursor : whitespace.end()]
            self._cursor = whitespace.end()
        if self._cursor == len(self._data):
            return
        probe: Final = self._data[self._cursor : self._cursor + _PROBE_SIZE]
        kind, size = _opening(probe, final or self._cursor + len(probe) < len(self._data))
        if kind == "wait":
            self._tail = probe
            self._cursor = len(self._data)
            return
        if kind == "text":
            self._mode = "text"
            return
        self._mode = "thinking"
        self._depth = 1
        self._cursor += size

    def _thinking(self, final: bool) -> Iterator[tuple[_Kind, str]]:
        token: Final = _TOKEN.search(self._data, self._cursor)
        if token is not None:
            if token.start() > self._cursor:
                yield "thinking", self._data[self._cursor : token.start()]
            self._depth += -1 if token.group().startswith("</") else 1
            self._cursor = token.end()
            if self._depth == 0:
                self._mode = "prefix"
            return
        end: Final = len(self._data) if final else max(self._cursor, len(self._data) - _TAIL_SIZE)
        if end > self._cursor:
            yield "thinking", self._data[self._cursor : end]
        self._tail = self._data[end:]
        self._cursor = len(self._data)


def normalize_inline_thinking_blocks(content: Sequence[object]) -> tuple[object, ...]:
    return tuple(chain.from_iterable(_normalized_block(block) for block in content))


def _normalized_block(value: object) -> tuple[object, ...]:
    if not isinstance(value, Mapping):
        return (value,)
    block: Final = _MAPPING.validate_python(value)
    text: Final = block.get("text")
    if block.get("type") != "text" or not isinstance(text, str) or not text:
        return (block,)
    return tuple(
        _parsed_block(block, kind, parts) for kind, parts in groupby(_ThinkingText().feed(text, True), _part_kind)
    )


def _part_kind(part: tuple[_Kind, str]) -> _Kind:
    return part[0]


def _parsed_block(block: Mapping[str, object], kind: _Kind, parts: Iterator[tuple[_Kind, str]]) -> Mapping[str, object]:
    text: Final = "".join(part[1] for part in parts)
    return {"type": "thinking", "thinking": text} if kind == "thinking" else {**block, "text": text}


class InlineThinkingStream:
    def __init__(self) -> None:
        self._offset = 0
        self._input_index: int | None = None
        self._output_index = 0
        self._template: Mapping[str, object] = {}
        self._parser = _ThinkingText()
        self._kind: _Kind | None = None
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
        return tuple(self._encode(item, headers if i == 0 else ()) for i, item in enumerate(self.transform(payload)))

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

    def transform(self, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        event_type: Final = payload.get("type")
        index: Final = payload.get("index")
        raw_block: Final = payload.get("content_block")
        block: Final = _MAPPING.validate_python(raw_block) if isinstance(raw_block, Mapping) else None
        if event_type == "content_block_start" and block is not None and block.get("type") == "text":
            if not isinstance(index, int):
                return (payload,)
            self._input_index = index
            self._template = payload
            self._parser = _ThinkingText()
            self._kind = None
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
                start: Final = self._start(
                    "text" if delta is not None and delta.get("type") == "citations_delta" else self._kind or "text"
                )
                return (*prefix, *start, {**payload, "index": self._output_index})
            if event_type == "content_block_stop":
                final: Final = self._feed("", final=True)
                empty: Final = self._start("text") if not self._emitted else ()
                stop: Final = self._close()
                self._input_index = None
                return (*final, *empty, *stop)
        return ({**payload, "index": index + self._offset},) if isinstance(index, int) else (payload,)

    def _start(self, kind: _Kind) -> tuple[Mapping[str, object], ...]:
        if self._open and self._kind == kind:
            return ()
        stop: Final = self._close()
        if self._emitted:
            self._offset += 1
        self._output_index = (self._input_index or 0) + self._offset
        self._emitted += 1
        self._open = True
        self._kind = kind
        original: Final = self._template.get("content_block")
        fields: Final[Mapping[str, object]] = (
            _MAPPING.validate_python(original) if isinstance(original, Mapping) else {}
        )
        block: Final = {"type": "thinking", "thinking": ""} if kind == "thinking" else {**fields, "text": ""}
        return (*stop, {**self._template, "index": self._output_index, "content_block": block})

    def _delta(self, text: str, kind: _Kind) -> tuple[Mapping[str, object], ...]:
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
        return tuple(chain.from_iterable(self._delta(part, kind) for kind, part in self._parser.feed(text, final)))
