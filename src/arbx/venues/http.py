# Scope: BOT_RUNTIME — Public HTTP support; never sends authenticated trade requests.
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from arbx.core.models import VenueHealth

RETRYABLE_STATUS_CODES = {429, 503}


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 2
    backoff_base_ms: int = 100
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.backoff_base_ms < 0:
            raise ValueError("backoff_base_ms cannot be negative")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class HttpResult:
    payload: dict[str, Any] | None
    status_code: int
    attempts: int
    health: VenueHealth


ResponseProvider = Callable[[str, Mapping[str, str]], HttpResponse | tuple[int, dict[str, Any] | None] | dict[str, Any]]
Sleeper = Callable[[float], None]
JitterSource = Callable[[], float]


class MockableHttpClient:
    def __init__(
        self,
        *,
        response_provider: ResponseProvider | None = None,
        retry_config: RetryConfig | None = None,
        sleeper: Sleeper | None = None,
        jitter_source: JitterSource | None = None,
    ) -> None:
        self.response_provider = response_provider
        self.retry_config = retry_config or RetryConfig()
        self.sleeper = sleeper or time.sleep
        self.jitter_source = jitter_source or random.random

    def get_json(
        self,
        url: str,
        *,
        venue: str,
        headers: Mapping[str, str] | None = None,
        retry_config: RetryConfig | None = None,
    ) -> HttpResult:
        active_retry_config = retry_config or self.retry_config
        request_headers = dict(headers or {})
        max_attempts = active_retry_config.max_retries + 1
        last_response = HttpResponse(status_code=0, payload=None)

        for attempt_number in range(1, max_attempts + 1):
            last_response = self._get_once(url, request_headers)
            if 200 <= last_response.status_code < 300:
                return HttpResult(
                    payload=last_response.payload,
                    status_code=last_response.status_code,
                    attempts=attempt_number,
                    health=_venue_health(venue, True, f"ok: attempts={attempt_number}"),
                )

            should_retry = last_response.status_code in RETRYABLE_STATUS_CODES
            if not should_retry or attempt_number == max_attempts:
                return HttpResult(
                    payload=last_response.payload,
                    status_code=last_response.status_code,
                    attempts=attempt_number,
                    health=_venue_health(
                        venue,
                        False,
                        f"degraded: status={last_response.status_code}; attempts={attempt_number}",
                    ),
                )

            self.sleeper(_backoff_seconds(active_retry_config, attempt_number, self.jitter_source))

        return HttpResult(
            payload=last_response.payload,
            status_code=last_response.status_code,
            attempts=max_attempts,
            health=_venue_health(venue, False, "degraded: retry loop exhausted"),
        )

    def _get_once(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        if self.response_provider is not None:
            return _coerce_response(self.response_provider(url, headers))

        try:
            request = Request(url, headers=dict(headers), method="GET")
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return HttpResponse(status_code=response.status, payload=_dict_payload(payload))
        except HTTPError as exc:
            payload = _payload_from_error(exc)
            return HttpResponse(status_code=exc.code, payload=payload)
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            return HttpResponse(status_code=0, payload=None)


def _backoff_seconds(
    retry_config: RetryConfig,
    attempt_number: int,
    jitter_source: JitterSource,
) -> float:
    base_seconds = retry_config.backoff_base_ms / 1000
    delay = base_seconds * (2 ** (attempt_number - 1))
    if retry_config.jitter:
        delay += base_seconds * jitter_source()
    return delay


def _coerce_response(response: HttpResponse | tuple[int, dict[str, Any] | None] | dict[str, Any]) -> HttpResponse:
    if isinstance(response, HttpResponse):
        return response
    if isinstance(response, tuple):
        status_code, payload = response
        return HttpResponse(status_code=status_code, payload=payload)
    return HttpResponse(status_code=200, payload=response)


def _payload_from_error(exc: HTTPError) -> dict[str, Any] | None:
    try:
        return _dict_payload(json.loads(exc.read().decode("utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _dict_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    return None


def _venue_health(venue: str, is_healthy: bool, reason: str) -> VenueHealth:
    return VenueHealth(
        venue=venue,
        is_healthy=is_healthy,
        last_checked=datetime.now(timezone.utc),
        reason=reason,
    )
