"""measure_ntp_offset_ms: stdlib SNTP query returns a float or None."""

import socket
import struct
import time

from arbx.capture import clock


class _FakeSocket:
    def __init__(self, response: bytes | None):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout_s):
        pass

    def sendto(self, data, addr):
        assert addr[1] == 123
        assert data[0] == 0x1B

    def recvfrom(self, size):
        if self._response is None:
            raise socket.timeout("timed out")
        return self._response, ("203.0.113.1", 123)


def _sntp_response(server_time_s: float) -> bytes:
    ntp_seconds = int(server_time_s) + clock._NTP_UNIX_DELTA
    frac = int((server_time_s % 1) * 2**32)
    packet = bytearray(48)
    struct.pack_into("!II", packet, 32, ntp_seconds, frac)  # receive ts
    struct.pack_into("!II", packet, 40, ntp_seconds, frac)  # transmit ts
    return bytes(packet)


def test_ntp_offset_returns_float_or_none(monkeypatch):
    # Server agrees with the host clock -> offset near zero.
    monkeypatch.setattr(
        clock.socket, "socket", lambda *a, **k: _FakeSocket(_sntp_response(time.time()))
    )
    offset = clock.measure_ntp_offset_ms()
    assert isinstance(offset, float)
    assert abs(offset) < 1000.0

    # Server 5s ahead of host -> host-minus-NTP is about -5000ms.
    monkeypatch.setattr(
        clock.socket, "socket",
        lambda *a, **k: _FakeSocket(_sntp_response(time.time() + 5.0)),
    )
    offset = clock.measure_ntp_offset_ms()
    assert offset is not None and -5300.0 < offset < -4700.0

    # Timeout / network failure -> None, never an exception.
    monkeypatch.setattr(clock.socket, "socket", lambda *a, **k: _FakeSocket(None))
    assert clock.measure_ntp_offset_ms() is None

    # Short/garbage response -> None.
    monkeypatch.setattr(clock.socket, "socket", lambda *a, **k: _FakeSocket(b"\x00" * 10))
    assert clock.measure_ntp_offset_ms() is None
