"""Single-send raw transport for the DeepSeek Anthropic Responses bridge."""

import asyncio
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

import httpx


@dataclass(frozen=True, slots=True)
class DeepSeekPreparedRequest:
    url: str
    headers: Mapping[str, str]
    body: bytes
    stream: bool = False


@dataclass(frozen=True, slots=True)
class DeepSeekRawResponse:
    response: httpx.Response


@dataclass(frozen=True, slots=True)
class DeepSeekRawFailure:
    category: str
    message: str
    status_code: int | None = None
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    body: bytes = b""


DeepSeekRawTransportResult = DeepSeekRawResponse | DeepSeekRawFailure


def freeze_deepseek_request(
    *,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, object] | bytes,
    stream: bool = False,
) -> DeepSeekPreparedRequest:
    # Match the shared Anthropic request-preparation serialization exactly.
    # Some compatibility gateways inspect the body before JSON parsing, so a
    # different compact representation is not wire-equivalent in practice.
    encoded_body = body if isinstance(body, bytes) else json.dumps(body).encode()
    return DeepSeekPreparedRequest(
        url=url,
        headers=MappingProxyType(dict(headers)),
        body=encoded_body,
        stream=stream,
    )


class DeepSeekResponsesRawTransport:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def send(self, request: DeepSeekPreparedRequest) -> DeepSeekRawTransportResult:
        response: httpx.Response | None = None
        try:
            prepared = self._client.build_request(
                "POST",
                request.url,
                headers=dict(request.headers),
                content=request.body,
            )
            response = await self._client.send(prepared, stream=request.stream, follow_redirects=False)
            # The bridge owns one frozen request per attempt. A redirect is a
            # second provider request, so surface every non-2xx response to
            # the owner instead of following it or attempting JSON decoding.
            if not 200 <= response.status_code < 300:
                body = await response.aread()
                await response.aclose()
                return DeepSeekRawFailure(
                    category="upstream_http_error",
                    message=body.decode(errors="replace"),
                    status_code=response.status_code,
                    headers=MappingProxyType(dict(response.headers)),
                    body=bytes(body),
                )
            return DeepSeekRawResponse(response=response)
        except asyncio.CancelledError:
            if response is not None:
                await response.aclose()
            raise
        except httpx.ConnectError as error:
            if response is not None:
                await response.aclose()
            return DeepSeekRawFailure(category="connect_error", message=str(error))
        except httpx.TimeoutException as error:
            if response is not None:
                await response.aclose()
            return DeepSeekRawFailure(category="timeout", message=str(error))
        except Exception as error:
            if response is not None:
                await response.aclose()
            return DeepSeekRawFailure(category="transport_error", message=str(error))


__all__ = [
    "DeepSeekPreparedRequest",
    "DeepSeekRawFailure",
    "DeepSeekRawResponse",
    "DeepSeekRawTransportResult",
    "DeepSeekResponsesRawTransport",
    "freeze_deepseek_request",
]
