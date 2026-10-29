# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/completed.py —— COMPLETED phase (注液完成终态)
# -----------------------------------------------------------------------------
# 进入路径: DOSING + CompletionConfirmed → action EmitCompletionPacket → COMPLETED.
# action 已经把 wire-status COMPLETED 包发了 (force=True), 并把 run_state 设 IDLE,
# 让 Pipeline 后续每帧 send 因闸门 (is_running=False, force=False) 不再发包.
#
# 退出路径 (只能由 Cmd 触发):
#   - CmdStart  → SEARCH_TUBE (action StartSession; 重置全部状态)
#   - CmdStop   → IDLE        (action EndSessionStopped)
#
# 每帧 handle: noop. 仍然抓帧, 但什么都不做 — 没有 sleep, 循环节奏由 Camera.grab() 的 1000ms
# 超时控制. wire 因 run_state=IDLE 闸门, 实际不出 bytes.
# =============================================================================
from __future__ import annotations

from typing import List

from ..events import Event
from ..context import Phase, PhaseContext, PhaseHandler


class CompletedPhase(PhaseHandler):
    phase = Phase.COMPLETED

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        return []
