# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/__init__.py —— 6 个 PhaseHandler 子类
# -----------------------------------------------------------------------------
# 一个文件一个 phase, 各自负责自己 phase 的"每帧计算":
#   idle.py          — IDLE phase  (handle 永远返回 [], 等 CmdStart)
#   search_tube.py   — SEARCH_TUBE (tube tracker.update + auto-expo gate)
#   lock_scale.py    — LOCK_SCALE  (find_center_scale_line + SCORE_BASE 自适应 + gap_stab)
#   dosing.py        — DOSING       (update_liquid_level + brightness 维持 + reach gate)
#   completed.py     — COMPLETED 终态 (handle 永远返回 [])
#   recheck_fail.py  — RECHECK_FAIL 终态 (slot 留好, 当前没人 emit RecheckFailed)
#
# 拆 6 个文件而不是单一 phases.py 的理由:
#   - 每个 handler 长度差异大 (idle ~20 行 vs lock_scale ~150 行)
#   - 不同 phase 的算法依赖不同 (lock_scale 依赖 center_scale + SCORE_BASE 状态;
#     dosing 依赖 li_det + auto_exposure; idle/completed/recheck_fail 啥都不依赖)
#   - 后续维护时按 phase 改, 文件作用域清晰
#
# make_handlers() 返回 dict, 由 main.py 调用, 传给 Pipeline 校验 + 用于 dispatch.
# =============================================================================
from typing import Dict

from ..phase import Phase, PhaseHandler

from .completed import CompletedPhase
from .dosing import DosingPhase
from .idle import IdlePhase
from .lock_scale import LockScalePhase
from .recheck_fail import RecheckFailPhase
from .search_tube import SearchTubePhase, SKIP_TUBE_HEIGHT_MIN


def make_handlers() -> Dict[Phase, PhaseHandler]:
    """构造 6-handler dict (Phase → 实例). 每个 Phase 一个 handler 实例.

    Pipeline.__init__ 会校验:
      - 6 个 Phase enum 值都在 dict 里 (KeyError check)
      - 每个 handler 实例的 .phase 字段 == 该 key (一致性 check)
    所以这里直接构造, 不用担心 Pipeline assertion 误失败.
    """
    return {
        Phase.IDLE:         IdlePhase(),
        Phase.SEARCH_TUBE:  SearchTubePhase(),
        Phase.LOCK_SCALE:   LockScalePhase(),
        Phase.DOSING:       DosingPhase(),
        Phase.COMPLETED:    CompletedPhase(),
        Phase.RECHECK_FAIL: RecheckFailPhase(),
    }


__all__ = [
    "IdlePhase", "SearchTubePhase", "LockScalePhase",
    "DosingPhase", "CompletedPhase", "RecheckFailPhase",
    "make_handlers", "SKIP_TUBE_HEIGHT_MIN",
]
