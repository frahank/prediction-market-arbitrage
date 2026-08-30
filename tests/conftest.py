# shared test configuration.
"""Test-suite defaults.

``TestClient`` defaults to ``base_url="http://testserver"``, which means every
request it makes carries ``Host: testserver``. The cockpit rejects non-loopback
``Host`` headers (``register_local_only_guard`` in ``arbx.ui.app``) to close off
DNS rebinding, so that default would turn the whole UI suite into 403s and hide
whatever the tests were actually asserting.

Tests do not care what host they appear to come from, so the loopback address is
set once here rather than at each of the many construction sites. Patching both
module attributes covers ``from fastapi.testclient import TestClient`` as well as
the Starlette import it re-exports; conftest is imported before test modules, so
the names they bind are already the patched subclass.
"""
from __future__ import annotations

import fastapi.testclient
import starlette.testclient

_LOOPBACK_BASE_URL = "http://127.0.0.1:8710"
_StarletteTestClient = starlette.testclient.TestClient


class LoopbackTestClient(_StarletteTestClient):
    """``TestClient`` that addresses the app as a loopback host by default."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("base_url", _LOOPBACK_BASE_URL)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]


starlette.testclient.TestClient = LoopbackTestClient  # type: ignore[misc]
fastapi.testclient.TestClient = LoopbackTestClient  # type: ignore[misc]
