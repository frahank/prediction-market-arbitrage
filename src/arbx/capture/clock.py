# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Scope: BOT_RUNTIME — Host-clock NTP offset measurement (pure-stdlib SNTP).
"""Measure the host wall clock's offset from an NTP reference.

Pure-stdlib SNTP (RFC 4330) over UDP — no new dependency. The offset is
*measured and recorded only* (schema §1: ``ntp_offset_ms`` = host wall clock
minus NTP reference); the bot never adjusts the host clock. See
docs/clock_discipline.md.
"""
from __future__ import annotations

import socket
import struct

# Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
_NTP_UNIX_DELTA = 2_208_988_800


def measure_ntp_offset_ms(server: str = "time.google.com", timeout_s: float = 2.0) -> float | None:
    """Host wall clock minus NTP reference, in milliseconds; None on failure.

    Standard SNTP offset: ((t1 − t0) + (t2 − t3)) / 2 is server-minus-host,
    where t0/t3 are host send/receive times and t1/t2 the server receive/
    transmit timestamps; the sign is flipped to match the schema convention.
    """
    import time

    packet = b"\x1b" + 47 * b"\x00"  # LI=0, VN=3, Mode=3 (client)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            t0 = time.time()
            sock.sendto(packet, (server, 123))
            data, _addr = sock.recvfrom(512)
            t3 = time.time()
    except OSError:
        return None
    if len(data) < 48:
        return None
    t1 = _ntp_timestamp(data, 32)  # server receive
    t2 = _ntp_timestamp(data, 40)  # server transmit
    if t2 <= 0:  # unsynchronized server / KoD
        return None
    server_minus_host_s = ((t1 - t0) + (t2 - t3)) / 2.0
    return -server_minus_host_s * 1000.0


def _ntp_timestamp(data: bytes, offset: int) -> float:
    seconds, fraction = struct.unpack("!II", data[offset:offset + 8])
    return seconds - _NTP_UNIX_DELTA + fraction / 2**32
