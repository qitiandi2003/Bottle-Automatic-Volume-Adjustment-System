# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/search_tube.py —— SEARCH_TUBE phase
# -----------------------------------------------------------------------------
# "找试管 + 一次性自动曝光"; phase 内不切相 — 试管 ok 时 emit TubeDetected, SM 切到 LOCK_SCALE.
#
# 每帧:
#   1) 高度 gate: ctx.last_cmd_height < SKIP_TUBE_HEIGHT_MIN → 设 ctx.last_tube_ok=False, 留在
#      SEARCH_TUBE. wire status 由 to_wire_status(last_tube_ok=False) 给 NO_TUBE.
#   2) tracker.update(bgr, make_viz=True) → tube_state.
#   3) ctx.last_tube_ok = bool(st.ok); ctx.tube_state = st; ctx.tube_state_fresh = True.
#   4) st.ok=False → 留在 SEARCH_TUBE (不 emit TubeLost; SearchTube/LockScale 都不发 TubeLost,
#      只设 ctx.last_tube_ok=False, 不切 phase, 不 reset 状态).
#   5) st.ok=True  → emit TubeDetected(tube_state=st). SM 单行 transition:
#      SEARCH_TUBE + TubeDetected → LOCK_SCALE + RunAutoExposureOnce (action 内部 idempotent).
#      Pipeline.run() 同帧 extra handle 跑一次 LockScalePhase, 复现"同帧 auto_expo + 找刻度"时序.
#
# Viz: tube_state.viz 已含 mask 半透红 + y0/y1 黄线 + 左右 x 蓝线; push 'vial_overlay' 占位.
# =============================================================================
from __future__ import annotations

from typing import List

from bottle_volume_control.diagnostics import stream as dbg_stream

from ..events import Event, TubeDetected
from ..context import Phase, PhaseContext, PhaseHandler


# skip_tube 高度下限 (mm). 物理意義: 上位機 CMD_START 後快速上升期 height=0 (假值),
# 上升結束才報真實值 (>=scale_h, 通常≥10). 此 gate 避開快速上升期不必要的試管檢測.
SKIP_TUBE_HEIGHT_MIN: float = 10.0


class SearchTubePhase(PhaseHandler):
    phase = Phase.SEARCH_TUBE

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        # ---- 1) 高度 gate ----
        # height < SKIP_TUBE_HEIGHT_MIN 时不调 tracker.update, 不 reset 状态, 不切 phase;
        # 设 last_tube_ok=False → wire NO_TUBE, 留在 SEARCH_TUBE.
        if ctx.last_cmd_height is not None and ctx.last_cmd_height < SKIP_TUBE_HEIGHT_MIN:
            ctx.last_tube_ok = False
            return []

        # ---- 2) tracker (StartSession 创建; 缺则 noop 等下一帧) ----
        if ctx.tube_tracker is None:
            ctx.last_tube_ok = False
            return []

        # POST-MERGE [A]: 把共享 gray_full 透传给 SlewTubeTracker.update; 它会再向下传给
        # detect_tube_hough_grad, 跳过其内部 cvtColor.
        gray_full = ctx.tree.gray_full if ctx.tree is not None else None
        try:
            st = ctx.tube_tracker.update(frame, make_viz=True, gray_full=gray_full)
        except Exception as e:
            if ctx.logger is not None:
                try: ctx.logger.error(f"[search_tube] tracker.update raised: {e}")
                except Exception: pass
            ctx.last_tube_ok = False
            return []

        # ---- 3) 同步 ctx ----
        ctx.tube_state = st
        ctx.last_tube_ok = bool(st and getattr(st, 'ok', False))
        # 标记 tracker 本帧已 update — Pipeline 同帧 extra-handle LockScalePhase 时会复用 ctx.tube_state,
        # 不重复跑 update (避免 SlewTubeTracker._frame_count 同帧多 ++).
        ctx.tube_state_fresh = True

        # ---- viz 推流 ----
        if st is not None and getattr(st, 'viz', None) is not None:
            try: dbg_stream.push('vial_overlay', st.viz)
            except Exception: pass

        # ---- DIAG: tracker.update 結果, 看試管首次被認 ok 那一刻的 height ----
        if ctx.logger is not None:
            try:
                _h = ctx.last_cmd_height
                _h_txt = "None" if _h is None else f"{_h:.2f}"
                ctx.logger.info(
                    f"[search_tube-trace] frame={ctx.frame_index} "
                    f"height={_h_txt} st.ok={ctx.last_tube_ok}"
                )
            except Exception:
                pass

        # ---- 4/5) 事件 emit ----
        # st.ok=False: 留在 SEARCH_TUBE, last_tube_ok=False 已设, 不 emit TubeLost.
        if not ctx.last_tube_ok:
            return []
        # st.ok=True: emit TubeDetected. SM 切 LOCK_SCALE + 跑 RunAutoExposureOnce action.
        if ctx.logger is not None:
            try: ctx.logger.info(f"[search_tube-trace] EMIT TubeDetected at height={ctx.last_cmd_height}")
            except Exception: pass
        return [TubeDetected(tube_state=st)]
