# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/lock_scale.py —— LOCK_SCALE phase
# -----------------------------------------------------------------------------
# 每帧 (顺序与早 return 语义见各小节注释):
#   1) 高度 gate: height < SKIP_TUBE_HEIGHT_MIN → last_tube_ok=False, return [].
#   2) tracker.update (跳过若 ctx.tube_state_fresh; 同帧 SearchTubePhase 已 update 过).
#   3) st.ok=False → return [] (不更新 last_tube_ok, 让 wire 保持上轮; 不切 phase).
#   4) center_inside_tube=False → last_tube_ok=False, ctx.l2s=LIQ_PRE_DET_L2S, return [].
#      不重置 score/lock 状态, 让下一帧 inside 恢复 True 时无缝继续.
#   5) find_center_scale_line(...).
#   6) score 处理顺序:
#      a) height >= scale_h 且 base 未 ready → append score (即使 not_found 也按 0 累计;
#         保證 p95 採樣基數穩定). 採滿 SCORE_BASE_SAMPLES_MAXLEN=10 同帧 freeze (p95*2).
#         不論 freeze 是否觸發, 一律 return [] — 採樣 path ctx.gap 保持 None (=wire 10000),
#         不出現實時 gap.
#      c) not res.found → return []. ctx.gap=None.
#      d) score <= thr → return []. ctx.gap=None.
#      e) score > thr → ctx.gap = -(line_x - cx) (实时刻度偏移, 负号约定).
#      f) gap_stab.update + is_stable; True → emit ScaleScored (SM 切 DOSING + FreezeScale).
#         False → return [] (gap 已设).
#
# wire 字段约定:
#   - 早 return path (height_gate / st.ok=False) ctx.gap=ctx.l2s=None → wire (10000, 10000).
#   - 主体 path (inside 通过后) ctx.l2s=LIQ_PRE_DET_L2S=50 — pre-detect 占位, 防止上位机
#     看到 10000 误判为"液面已到刻度". DOSING first_frame 才换成实时 l2s.
#   - ctx.gap 仅在 score>thr+found 时设实时值 (= -(line_x-cx), 负号约定); 其它情况 None.
#     LOCK_SCALE 阶段用负号, DOSING 重新计算用正号 (DosingPhase first_frame 重设).
# =============================================================================
from __future__ import annotations

import time
from collections import deque
from typing import List

import numpy as np

import debug_stream as dbg_stream
import center_scale
import rt_config

from ..event import Event, ScaleScored
from ..phase import Phase, PhaseContext, PhaseHandler
from ..state_machine import LIQ_PRE_DET_L2S
from .. import viz as _viz
from .search_tube import SKIP_TUBE_HEIGHT_MIN


# SCORE_BASE 採樣:
# 觸發: ctx.last_cmd_height >= ctx.last_cmd_scale_height (協議 "預設容量瓶刻度線高度" float).
# 上位機 CMD_START 後快速上升期間 height=0 (假值), 上升結束瞬間 height 跳到 scale_h — 即觸發點.
# 採滿 SCORE_BASE_SAMPLES_MAXLEN 個樣本即同幀 freeze; 採樣 path 一律 return.
# Freeze: base = p95(samples), threshold = base * SCORE_BASE_THR_MULTIPLIER.
SCORE_BASE_SAMPLES_MAXLEN: int = 10
SCORE_BASE_THR_MULTIPLIER: float = 0.95 #锁定刻度线的阈值参数受环境光影响，默认2.0可能锁定不了刻度线


def _height_reached_scale(ctx: PhaseContext) -> bool:
    """採樣觸發: 機械臂高度已到達上位機"預設容量瓶刻度線高度". scale_h=None → False."""
    h = ctx.last_cmd_height
    sh = ctx.last_cmd_scale_height
    if h is None or sh is None:
        return False
    return h >= sh


def _freeze_score_base(ctx: PhaseContext) -> None:
    """采样窗口结束时定 base = p95(samples), threshold = base * SCORE_BASE_THR_MULTIPLIER."""
    if ctx.score_base_samples is None or len(ctx.score_base_samples) == 0:
        return
    base = float(np.percentile(list(ctx.score_base_samples), 75))
    ctx.score_base_value = base
    ctx.score_base_ready = True
    if ctx.logger is not None:
        try:
            ctx.logger.info(f"[lock_scale] SCORE_BASE freeze: base={base:.1f} thr={base*SCORE_BASE_THR_MULTIPLIER:.1f}")
        except Exception:
            pass


def _score_threshold(ctx: PhaseContext) -> float:
    """未 ready → 10000.0 (永不过阈值, 不会触发 gap)."""
    if not ctx.score_base_ready or ctx.score_base_value is None:
        return 10000.0
    return float(ctx.score_base_value) * SCORE_BASE_THR_MULTIPLIER


class LockScalePhase(PhaseHandler):
    phase = Phase.LOCK_SCALE

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        # 入口: ctx.gap/l2s 复位 None — 早 return 路径 (height_gate / st.ok=False) 直接走 wire 10000;
        # 主体路径 (inside 通过后) 会再把 ctx.l2s 改成 LIQ_PRE_DET_L2S=50.
        # 仅 score>thr+found 时 ctx.gap 改实时值.
        ctx.gap = None
        ctx.l2s = None

        # ---- DIAG: 入口診斷 — 觀察採樣 path 觸發/命中 ----
        # 採樣命中要求: height >= scale_h AND samples_n < SCORE_BASE_SAMPLES_MAXLEN AND not ready.
        if ctx.logger is not None:
            try:
                _h_dbg = ctx.last_cmd_height
                _sh_dbg = ctx.last_cmd_scale_height
                _h_txt = "None" if _h_dbg is None else f"{_h_dbg:.2f}"
                _sh_txt = "None" if _sh_dbg is None else f"{_sh_dbg:.2f}"
                _ready = bool(ctx.score_base_ready)
                _n = len(ctx.score_base_samples) if ctx.score_base_samples is not None else 0
                ctx.logger.info(
                    f"[lock_scale-trace] enter frame={ctx.frame_index} "
                    f"height={_h_txt} scale_h={_sh_txt} ready={_ready} samples_n={_n}/{SCORE_BASE_SAMPLES_MAXLEN}"
                )
            except Exception:
                pass

        # ---- 1) 高度 gate ----
        # 早 return: ctx.gap=None / ctx.l2s=None → wire (NO_TUBE, 10000, 10000).
        if ctx.last_cmd_height is not None and ctx.last_cmd_height < SKIP_TUBE_HEIGHT_MIN:
            if ctx.logger is not None:
                try: ctx.logger.info(f"[lock_scale-trace] EARLY RETURN: height_gate (height={ctx.last_cmd_height:.2f} < {SKIP_TUBE_HEIGHT_MIN})")
                except Exception: pass
            ctx.last_tube_ok = False
            return []

        if ctx.tube_tracker is None:
            ctx.last_tube_ok = False
            return []

        # POST-MERGE 共享 trunk gray (byte-exact, 不改算法)
        gray_full = ctx.tree.gray_full if ctx.tree is not None else None

        # ---- 2) tracker ----
        # 同帧 extra-handle: SearchTubePhase 已 update tracker 并设 ctx.tube_state_fresh=True;
        # 这里复用 ctx.tube_state, 不重复 update (避免 SlewTubeTracker._frame_count 同帧多 ++).
        if ctx.tube_state_fresh and ctx.tube_state is not None:
            st = ctx.tube_state
        else:
            try:
                st = ctx.tube_tracker.update(frame, make_viz=True, gray_full=gray_full)
            except Exception as e:
                if ctx.logger is not None:
                    try: ctx.logger.error(f"[lock_scale] tracker.update raised: {e}")
                    except Exception: pass
                ctx.last_tube_ok = False
                return []
            ctx.tube_state = st
            ctx.tube_state_fresh = True

        cur_st_ok = bool(st and getattr(st, 'ok', False))
        # st.ok=False 時 *不* 更新 ctx.last_tube_ok — 让 to_wire_status 看到的是上轮值 (試管曾經
        # ok 過 → 保持 INIT, 從未 ok → StartSession 設的 False → wire NO_TUBE).
        if cur_st_ok:
            ctx.last_tube_ok = True

        if st is not None and getattr(st, 'viz', None) is not None:
            try: dbg_stream.push('vial_overlay', st.viz)
            except Exception: pass

        # ---- 3) st.ok=False ----
        # 留在 LOCK_SCALE phase, 不重置任何状态; 不繼續處理 inside/find — 它們需要 st.ok=True.
        if not cur_st_ok:
            if ctx.logger is not None:
                try: ctx.logger.info(f"[lock_scale-trace] EARLY RETURN: cur_st_ok=False (height={ctx.last_cmd_height})")
                except Exception: pass
            return []

        cx = frame.shape[1] // 2

        # ---- 4) center_inside_tube ----
        # inside=False → last_tube_ok=False (wire NO_TUBE), l2s=50 (已进 process 主体), return [].
        # 不重置 score_base_samples / locked_* — 让下一帧 inside 恢复 True 时无缝继续找刻度.
        try:
            inside = center_scale.center_inside_tube(st, cx, st.y0 + 3, st.y1 - 3)
        except Exception:
            inside = False
        if not inside:
            if ctx.logger is not None:
                try: ctx.logger.info(f"[lock_scale-trace] EARLY RETURN: inside=False (height={ctx.last_cmd_height})")
                except Exception: pass
            ctx.last_tube_ok = False
            # inside=False 已进 process 主体, l2s 走 50 占位 (跟早 return path 的 10000 区分).
            ctx.l2s = LIQ_PRE_DET_L2S
            return []

        # 主体: SCORE_BASE 採樣 / not found / score<=thr / score>thr 等所有 send path 都 wire l2s=50.
        ctx.l2s = LIQ_PRE_DET_L2S

        # ---- 5) find_center_scale_line ----
        try:
            res = center_scale.find_center_scale_line(
                frame, cx, st.y0 + 3, st.y1 - 3, st.mask,
                band=50, lambda_center=1.0, conf_min=0.15,
                score_min=None, viz=True,
                base_viz=getattr(st, 'viz', None),
                gray_full=gray_full,
            )
        except Exception as e:
            if ctx.logger is not None:
                try: ctx.logger.error(f"[lock_scale] find_center_scale_line raised: {e}")
                except Exception: pass
            return []

        if res is None:
            return []

        # viz: tube_edges_binary + scale_sobel_x_binary (不论 found 都 push, inside=False 路径已 return).
        try:
            tube_edges = _viz.make_tube_edges_viz(frame, st)
            if tube_edges is not None:
                dbg_stream.push('tube_edges_binary', tube_edges)
        except Exception:
            pass
        try:
            sx_viz = _viz.make_scale_sobel_x_viz(frame, st, res)
            if sx_viz is not None:
                dbg_stream.push('scale_sobel_x_binary', sx_viz)
        except Exception:
            pass

        # 不论 found 与否都先取 score (用于 SCORE_BASE 采样).
        score = float(res.get('score') or 0.0)

        # ---- 6a) SCORE_BASE 採樣 ----
        # 觸發: height >= scale_h AND not ready. score 即使 not found 也累計 (= 0.0 或 column-energy 噪聲);
        # p95 採樣基數穩定. 採滿 SCORE_BASE_SAMPLES_MAXLEN 個即同幀 freeze; 採樣 path 一律 return [] —
        # 保證採樣中 wire gap 恆 None (=10000 placeholder), 不出現實時 gap 污染上位機判斷.
        _h_reached = _height_reached_scale(ctx)
        if ctx.logger is not None:
            try:
                _h = ctx.last_cmd_height
                _sh = ctx.last_cmd_scale_height
                _h_txt = "None" if _h is None else f"{_h:.2f}"
                _sh_txt = "None" if _sh is None else f"{_sh:.2f}"
                ctx.logger.info(
                    f"[lock_scale-trace] score-base check: height={_h_txt} scale_h={_sh_txt} "
                    f"reached={_h_reached} ready={ctx.score_base_ready} "
                    f"score={score:.1f} res_found={bool(res.get('found'))}"
                )
            except Exception:
                pass
        if _h_reached and not ctx.score_base_ready:
            if ctx.score_base_samples is not None:
                ctx.score_base_samples.append(score)
                # 採滿即 freeze (同幀但不繼續走 detect — 確保採樣中 wire gap 恆 None)
                if len(ctx.score_base_samples) >= SCORE_BASE_SAMPLES_MAXLEN:
                    _freeze_score_base(ctx)
            if rt_config.LOCK_SCALE_VERBOSE and ctx.logger is not None:
                try:
                    h = ctx.last_cmd_height
                    h_txt = f"{h:.2f}" if h is not None else "None"
                    n = len(ctx.score_base_samples) if ctx.score_base_samples is not None else 0
                    ctx.logger.info(
                        f"[lock_scale] 刻度基线采样中 高度={h_txt} 得分={score:.1f} "
                        f"样本数={n}/{SCORE_BASE_SAMPLES_MAXLEN} found={bool(res.get('found'))}"
                    )
                except Exception:
                    pass
            return []

        # ---- 6c) not found ----
        if not res.get('found'):
            if rt_config.LOCK_SCALE_VERBOSE and ctx.logger is not None:
                try: ctx.logger.info("[lock_scale] 未找到刻度线（允许为空）")
                except Exception: pass
            return []

        line_x = int(res.get('x'))
        dx = line_x - cx
        score_thr = _score_threshold(ctx)

        if rt_config.LOCK_SCALE_VERBOSE and ctx.logger is not None:
            try:
                base_txt = "None" if ctx.score_base_value is None else f"{float(ctx.score_base_value):.1f}"
                conf_v = res.get('conf', 0)
                ctx.logger.info(
                    f"[lock_scale] 刻度x={line_x} 偏移dx={dx:+d}px | 得分={score:.1f} "
                    f"base={base_txt} 阈值={score_thr:.1f} 置信度={float(conf_v):.2f}"
                )
            except Exception:
                pass

        # ---- 6d) score <= thr ----
        if score <= score_thr:
            return []

        # ---- 6e) score > thr: 实时 gap (负号约定; DOSING first_frame 重新算正号) ----
        gap = int(-dx)
        ctx.gap = gap

        # ---- 6f) gap_stab.update + is_stable ----
        stable = False
        ns_log: int = 0
        dur_log: float = 0.0
        if ctx.gap_stab is not None:
            try:
                ctx.gap_stab.update(time.monotonic(), gap)
                st_ok, _gmin, _gmax, _ns, _dur = ctx.gap_stab.is_stable()
                stable = bool(st_ok)
                ns_log = int(_ns)
                dur_log = float(_dur)
            except Exception:
                stable = False

        # gap_stab 稳定窗口状态 — ns_log 是 gap_stability 10s 窗口的样本数 (~28-30 @ 30fps),
        # 跟 SCORE_BASE 採樣 samples_n=10/10 是兩個概念, 故文案改 "窗口样本数" 显式区分.
        if rt_config.LOCK_SCALE_VERBOSE and ctx.logger is not None:
            try: ctx.logger.info(f"[lock_scale] gap_stab 稳定时长={dur_log:.2f}s 窗口样本数={ns_log}")
            except Exception: pass

        if not stable:
            return []

        # 全部条件过 → emit ScaleScored. SM 切 DOSING + FreezeScale (refine + 缓存; calibrate 留给
        # DOSING 第一帧). 这一帧 wire 仍发 gap=ctx.gap (实时), l2s=50 — to_wire_status 收 dosing_first_frame
        # 标志映射成 INIT 字节.
        return [ScaleScored(score=score, line_x=line_x)]


def init_score_base_state(ctx: PhaseContext) -> None:
    """StartSession action 调用. 重置 SCORE_BASE 采样状态. deque 在这里创建 (避免 dataclass default_factory)."""
    ctx.score_base_samples = deque(maxlen=SCORE_BASE_SAMPLES_MAXLEN)
    ctx.score_base_value = None
    ctx.score_base_ready = False
