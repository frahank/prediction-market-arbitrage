# SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
# SPDX-License-Identifier: MIT
# Runtime safety primitives.
"""Execution package.

Only the kill switch lives here. This package contains no order clients or
order-mutation capability.
"""

from arbx.exec.killswitch import KillSwitch, KillSwitchEngaged

__all__ = ["KillSwitch", "KillSwitchEngaged"]
