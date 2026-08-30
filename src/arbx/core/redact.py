# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Shared redaction helpers for logs, UI, and CLI output.
"""Value-targeted redaction for anything that leaves the process.

The rule that matters: **redact the secret, not the label**. A pattern that
matches the word ``secret`` and blanks *that word* leaves the adjacent value
untouched, which is worse than useless — the output looks sanitized and is not.
Every rule here therefore rewrites the right-hand side of a match and leaves the
key readable, so redacted output is still debuggable.

Covered: PEM/DER blocks, JWTs, ``Authorization``/``Cookie``/``KALSHI-ACCESS-*``
header values, ``key: value`` and ``key=value`` pairs whose key names a
credential, and raw base64 runs. Values registered via :func:`register_secret`
are additionally matched exactly, wherever they appear.

Deliberately NOT covered: long hex and long decimal runs. Polymarket condition
ids (``0x`` + 64 hex) and CLOB token ids (77-digit decimals) are *public*
identifiers that appear throughout market data and logs; scrubbing them by
entropy alone would make captured output unreadable while protecting nothing.
The base64 rule below requires a base64-only character to fire, which those
identifiers never contain.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Mapping

REDACTED = "[REDACTED]"

# Key names that indicate the *following* value is credential material.
_SENSITIVE_KEY = (
    r"(?:api[_-]?key(?:[_-]?id)?|api[_-]?secret|client[_-]?secret|secret"
    r"|access[_-]?token|refresh[_-]?token|id[_-]?token|token"
    r"|passphrase|password|passwd|pwd"
    r"|private[_-]?key|wallet[_-]?private[_-]?key|signing[_-]?key"
    # "authorization" and "cookie" are handled by their own header rules, which
    # keep the scheme readable; listing them here would blank "Bearer" too.
    r"|signature|credential|session)"
)

# Mapping keys are matched more broadly than inline text: in a dict the key and
# its value are unambiguously paired, so there is no "Bearer" scheme to preserve
# and no risk of blanking a word out of running prose.
_SENSITIVE_MAPPING_KEY = _SENSITIVE_KEY[:-1] + r"|authorization|cookie|auth|bearer)"

# Public identifiers that happen to contain a sensitive-looking word. These are
# market data, not secrets, and redacting them would blind the capture logs.
_PUBLIC_KEY_EXEMPT = frozenset(
    {
        "token_id",
        "yes_token_id",
        "no_token_id",
        "condition_id",
        "market_id",
        "kalshi_market_id",
        "polymarket_market_id",
        "token_ids",
    }
)

_PEM_BLOCK = re.compile(r"-----BEGIN[^-]{0,64}-----.*?-----END[^-]{0,64}-----", re.DOTALL)

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")

# "Authorization: Bearer xyz" / "authorization=Basic xyz" — keep the scheme.
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)((?:bearer|basic|token|digest)\s+)?(\S+)"
)

# Cookie headers carry everything to end of line.
_COOKIE_HEADER = re.compile(r"(?i)^([ \t]*(?:set-)?cookie\s*:\s*).+$", re.MULTILINE)

_KALSHI_HEADER = re.compile(
    r"(?i)\b(KALSHI-ACCESS-(?:KEY|SIGNATURE)\s*[:=]\s*)(\S+)"
)

# key: value / key = value / "key": "value". The value runs to the next
# delimiter so surrounding JSON/YAML structure survives.
_KEY_VALUE = re.compile(
    r"(?i)(?P<key>[\"']?[A-Za-z0-9_.\-]*" + _SENSITIVE_KEY + r"[A-Za-z0-9_.\-]*[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"',;}\]]+)"
)

# Raw base64 runs. Requires a '+', '/' or '=' so hex ids and decimal token ids
# (which have none) can never match.
_BASE64_RUN = re.compile(r"\b(?=[A-Za-z0-9+/]*[+/=])[A-Za-z0-9+/]{40,}={0,2}")

# Exact credential values registered at store/load time. Registration is the
# second layer, not the only one — the patterns above must stand alone.
_REGISTERED_LOCK = threading.Lock()
_REGISTERED_SECRETS: set[str] = set()
# Long enough that a registered value cannot blank ordinary words out of output.
_MIN_SECRET_LENGTH = 12
_MAX_REGISTERED_SECRETS = 256


def register_secret(value: str) -> None:
    """Register an exact credential value so redaction removes it everywhere."""
    if not isinstance(value, str) or len(value) < _MIN_SECRET_LENGTH:
        return
    with _REGISTERED_LOCK:
        if len(_REGISTERED_SECRETS) < _MAX_REGISTERED_SECRETS:
            _REGISTERED_SECRETS.add(value)


def _registered_snapshot() -> tuple[str, ...]:
    """Longest first, so a secret containing another leaves no tail behind."""
    with _REGISTERED_LOCK:
        return tuple(sorted(_REGISTERED_SECRETS, key=len, reverse=True))


def _is_exempt_key(key: str) -> bool:
    return key.strip("\"'").lower() in _PUBLIC_KEY_EXEMPT


def _already_redacted(value: str) -> bool:
    """An earlier rule got here first; re-running would double the marker."""
    return value.lstrip("\"'").startswith(REDACTED[:-1])


def _redact_key_value(match: re.Match[str]) -> str:
    key = match.group("key")
    if _is_exempt_key(key) or _already_redacted(match.group("value")):
        return match.group(0)
    return f"{key}{match.group('sep')}{match.group('quote')}{REDACTED}"


def _redact_auth_header(match: re.Match[str]) -> str:
    if _already_redacted(match.group(3)):
        return match.group(0)
    scheme = match.group(2) or ""
    return f"{match.group(1)}{scheme}{REDACTED}"


def redact_text(value: str) -> str:
    """Remove credential *values* from ``value``; labels stay readable."""
    if not isinstance(value, str) or not value:
        return value
    result = _PEM_BLOCK.sub(REDACTED, value)
    result = _JWT.sub(REDACTED, result)
    result = _COOKIE_HEADER.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    result = _KALSHI_HEADER.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    result = _AUTH_HEADER.sub(_redact_auth_header, result)
    result = _KEY_VALUE.sub(_redact_key_value, result)
    result = _BASE64_RUN.sub(REDACTED, result)
    for secret in _registered_snapshot():
        if secret in result:
            result = result.replace(secret, REDACTED)
    return result


def _key_is_sensitive(key: str) -> bool:
    if _is_exempt_key(key):
        return False
    return re.search(_SENSITIVE_MAPPING_KEY, key, re.IGNORECASE) is not None


def redact_jsonable(value: Any) -> Any:
    """Redact a JSON-shaped structure.

    A mapping key naming a credential redacts its whole value, whatever the
    value's shape — that is the case the string patterns cannot see, because by
    the time a dict is serialized the key and value may be far apart.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED if _key_is_sensitive(str(key)) else redact_jsonable(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_jsonable(item) for item in value]
    return value
