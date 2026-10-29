# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/idle.py —— IDLE phase (等 CmdStart)
# -----------------------------------------------------------------------------
# IDLE 不主动做任何事. CmdStart 由 UpperLink emit 后, SM transition 表把
# IDLE → SEARCH_TUBE (action StartSession). handle() 永远返回 [].
# =============================================================================
from __future__ import annotations

from typing import List

from ..event import Event
from ..phase import Phase, PhaseContext, PhaseHandler


class IdlePhase(PhaseHandler):
    phase = Phase.IDLE

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        return []

    def on_enter(self, ctx: PhaseContext) -> None:
        """进入 IDLE 时打一次 log. 两个触发场景:
          (a) 进程 boot → IDLE  : Pipeline.run() 启动时手动调一次 (StateMachine 构造时
              self.current=IDLE, 不会自动触发 on_enter; Pipeline 补一次).
          (b) 任意 ACTIVE/TERMINAL phase → IDLE (CmdPause / CmdStop / 完成后 run_state→IDLE):
              SM 切相时自动触发.

        刻意不节流刷 — 用户决策"开程序进入 IDLE 后进行一次打印即可", 这个 log 等同于
        "boot→Init 状态切换" 的可见提示, 不每帧滚动.
        """
        if ctx.logger is not None:
            try:
                ctx.logger.info("[idle] 进入空闲状态, 等待 CmdStart")
            except Exception:
                pass
