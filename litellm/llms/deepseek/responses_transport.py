"""Single-send raw transport for the DeepSeek Anthropic Responses bridge."""

import asyncio
import json
from dataclasses import dataclass
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


DeepSeekRawTransportResult = DeepSeekRawResponse | DeepSeekRawFailure


def freeze_deepseek_request(
    *,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, object] | bytes,
    stream: bool = False,
) -> DeepSeekPreparedRequest:
    encoded_body = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode()
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
            response = await self._client.send(prepared, stream=request.stream)
            if response.status_code >= 400:
                body = await response.aread()
                await response.aclose()
                return DeepSeekRawFailure(
                    category="upstream_http_error",
                    message=body.decode(errors="replace"),
                    status_code=response.status_code,
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
