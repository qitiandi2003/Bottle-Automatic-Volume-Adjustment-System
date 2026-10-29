# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/recheck_fail.py —— RECHECK_FAIL phase (复核失败终态)
# -----------------------------------------------------------------------------
# 当前状态 (2026-05-07): 还没有 PhaseHandler 会 emit RecheckFailed 事件. 旧
# detector.py:_finish_completion (L1249) 通过 _run_recheck (L870) 触发 RECHECK_FAIL,
# 但当前 locked-mode 主链路走 _finish_completion_only (L1278), 不调复核.
#
# 这里把 PhaseHandler 槽留好, 待后续接复核逻辑时:
#   - DosingPhase 在 CompletionConfirmed 之前 / 之后调 _run_recheck 等价代码;
#   - recheck 失败 → emit RecheckFailed 而非 CompletionConfirmed;
#   - SM 命中 DOSING + RecheckFailed → action EmitRecheckFailPacket → RECHECK_FAIL.
#
# 每帧 handle: noop (与 COMPLETED 同).
# 退出路径同 COMPLETED: CmdStart 或 CmdStop.
# =============================================================================
from __future__ import annotations

from typing import List

from ..events import Event
from ..context import Phase, PhaseContext, PhaseHandler


class RecheckFailPhase(PhaseHandler):
    phase = Phase.RECHECK_FAIL

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        return []
