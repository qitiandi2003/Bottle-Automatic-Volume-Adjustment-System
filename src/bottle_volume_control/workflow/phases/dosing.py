# -*- coding: utf-8 -*-
# =============================================================================
# framework/phases/dosing.py —— DOSING phase (锁定后液面跟踪)
# -----------------------------------------------------------------------------
# 每帧分两阶段:
#
#   First frame (ctx.dosing_first_frame=True; 由 FreezeScale action 设置):
#     - 跑 calibrate_locked_frame → ctx.locked_res (含 sobel_proj_base, 给 update_liquid_level 用)
#     - 抓 first_frame_brightness baseline (后续帧亮度维持要用)
#     - 重新计算 ctx.gap = scale_line_x - cx (正号; LOCK_SCALE 阶段用的是负号)
#     - 重置 ctx.liquid_entered=False, ctx.l2s=10000
#     - **不**跑 update_liquid_level. 不 emit 事件.
#     - 设 ctx.dosing_first_frame=False
#
#   后续帧:
#     1) brightness 维持: 当前帧 gray-median vs first_frame_brightness, 偏差 > 5
#        → 按比例调相机曝光 (clamp [3000, 40000]us). 用 auto_exposure._get/_set_manual_exposure.
#     2) update_liquid_level(bgr, locked_res, li_state) → (pinned, raw, decision, peak, signals).
#        li_state dict 就地修改 (raw_history / pinned_max_x / last_decision 等).
#     3) liquid_entered 粘性 True (一旦 pinned 出现就不回 False; 仅 first_frame 重置).
#        l2s = li_state['pinned_max_x'] - scale_line_x (条件: liquid_entered AND
#        li_state['pinned_max_x'] is not None); 否则 10000 占位.
#     4) emit LiquidUpdated(l2s, peak_strength) 每帧 (telemetry).
#     5) is_reached = (liquid_entered AND li_state_pinned!=None AND l2s <= LIQ_REACH_THR_PX)
#        → emit CompletionConfirmed (SM 转 COMPLETED, action EmitCompletionPacket 发包 + run_state→IDLE).
#
# 完成后 SM 转终态 COMPLETED, 不会自动回 SEARCH_TUBE; CmdStart 才能再做一轮 (StartSession 重置状态).
# =============================================================================
from __future__ import annotations

from typing import List

import cv2
import numpy as np

from bottle_volume_control.camera import exposure as auto_exposure
from bottle_volume_control.diagnostics import stream as dbg_stream
from bottle_volume_control.vision import liquid as li_det
from bottle_volume_control import config as rt_config

from ..events import CompletionConfirmed, Event, LiquidUpdated
from ..context import Phase, PhaseContext, PhaseHandler
from ..state_machine import LIQ_REACH_THR_PX
from bottle_volume_control.diagnostics import render as _viz


class DosingPhase(PhaseHandler):
    phase = Phase.DOSING

    def handle(self, frame, ctx: PhaseContext) -> List[Event]:
        # ---- First frame in DOSING ----
        # FreezeScale action 在 LOCK_SCALE→DOSING 跨相时设 ctx.dosing_first_frame=True.
        # 这一帧 calibrate + 抓 brightness baseline, 不跑 update_liquid_level.
        if ctx.dosing_first_frame:
            if ctx.scale_line_x is None:
                # FreezeScale action 没填 scale_line_x = 异常 (理论不应到达). 标 done 防卡死.
                if ctx.logger is not None:
                    try: ctx.logger.error("[dosing] first_frame: missing scale_line_x; abort")
                    except Exception: pass
                ctx.dosing_first_frame = False
                return []

            x_lock = int(ctx.scale_line_x)
            locked_y0 = int(ctx.locked_y0 if ctx.locked_y0 is not None else 0)
            locked_y1 = int(ctx.locked_y1 if ctx.locked_y1 is not None else 0)
            locked_detection_start_x = int(
                ctx.locked_detection_start_x if ctx.locked_detection_start_x is not None else 0
            )

            try:
                locked_res = li_det.calibrate_locked_frame(
                    frame, x_lock, locked_y0, locked_y1, locked_detection_start_x,
                )
            except Exception as e:
                # 异常被吃掉, dosing_first_frame 保持 True 让下一帧重试.
                if ctx.logger is not None:
                    try: ctx.logger.error(f"[dosing] first_frame calibrate raised: {e}; will retry")
                    except Exception: pass
                ctx.l2s = 10000
                return []
            ctx.locked_res = locked_res

            # gap 重新计算成正号 (LOCK_SCALE 阶段是负号; 上位机看到的 gap 符号会切换, 既有协议).
            cx = frame.shape[1] // 2
            scale_line_x_locked = int(locked_res.get('scale_line_x', x_lock))
            ctx.gap = scale_line_x_locked - cx

            # liquid_entered 在 first_frame 重置 — 之后只能由 pinned!=None 设 True (粘性).
            ctx.liquid_entered = False

            # first_frame_brightness baseline
            try:
                if ctx.tree is not None and ctx.tree.gray_full is not None:
                    gray_ref = ctx.tree.gray_full
                else:
                    gray_ref = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                ctx.first_frame_brightness = float(np.median(gray_ref))
            except Exception:
                ctx.first_frame_brightness = None

            # first_frame wire l2s=10000 (LOCK_SCALE 阶段是 50 占位; 这里换成 10000 表 pre-pinned).
            ctx.l2s = 10000
            ctx.dosing_first_frame = False

            if ctx.logger is not None:
                try:
                    ctx.logger.info(
                        f"[dosing] first_frame done: x_lock={x_lock} y0/y1={locked_y0}/{locked_y1} "
                        f"brightness={ctx.first_frame_brightness}"
                    )
                except Exception:
                    pass
            # first_frame 不跑 update_liquid_level, 不 emit 事件.
            return []

        # ---- 后续帧 ----

        # ---- 0) 前置检查 (calibration 必须已完成) ----
        if ctx.locked_res is None or ctx.li_state is None:
            if ctx.logger is not None:
                try: ctx.logger.error("[dosing] missing locked_res/li_state; abort frame")
                except Exception: pass
            return []

        # ---- 1) brightness 维持 (cam None 时跳过) ----
        if ctx.first_frame_brightness is not None and ctx.cam is not None:
            try:
                if ctx.tree is not None and ctx.tree.gray_full is not None:
                    gray_cur = ctx.tree.gray_full
                else:
                    gray_cur = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                cur = float(np.median(gray_cur))
                if cur > 1.0 and abs(cur - ctx.first_frame_brightness) > 5.0:
                    prev_exp = auto_exposure._get_exposure_us(ctx.cam)
                    if prev_exp is not None:
                        ratio = ctx.first_frame_brightness / cur
                        new_exp = max(3000.0, min(40000.0, prev_exp * ratio))
                        auto_exposure._set_manual_exposure(ctx.cam, new_exp)
            except Exception:
                pass

        # ---- 2) liquid update ----
        try:
            pinned, raw, decision, peak, signals = li_det.update_liquid_level(
                frame, ctx.locked_res, ctx.li_state,
            )
        except Exception as e:
            if ctx.logger is not None:
                try: ctx.logger.error(f"[dosing] update_liquid_level raised: {e}")
                except Exception: pass
            return []

        # ---- 3) liquid_entered 粘性 + l2s 计算 ----
        # 注意: l2s 条件用 li_state['pinned_max_x'] (li_det 内部维护) 而不是函数本地 pinned —
        # 两者语义不同: pinned 是本帧返回, li_state['pinned_max_x'] 是内部记录 (跨帧持久).
        if pinned is not None:
            ctx.liquid_entered = True

        scale_line_x = int(ctx.locked_res.get('scale_line_x', 0))
        li_state_pinned = ctx.li_state.get('pinned_max_x') if ctx.li_state else None
        if ctx.liquid_entered and li_state_pinned is not None:
            l2s = int(li_state_pinned - scale_line_x)
        else:
            l2s = 10000
        ctx.l2s = l2s

        if rt_config.DOSING_VERBOSE and ctx.logger is not None:
            try:
                peaks_lmr = (ctx.li_state.get('last_peaks_lmr', []) if ctx.li_state else []) or []
                peaks_str = '/'.join(f'{x}:{s:.0f}' for x, s in peaks_lmr) if peaks_lmr else '-'
                last_raw = ctx.li_state.get('last_raw') if ctx.li_state else None
                peak_v = ctx.li_state.get('last_peak_strength', 0.0) if ctx.li_state else 0.0
                ctx.logger.info(
                    f"[dosing] gap={ctx.gap} l2s={l2s} "
                    f"raw={last_raw} pinned={pinned} "
                    f"peak={float(peak_v or 0.0):.0f} "
                    f"gate={decision} peaks={peaks_str}"
                )
            except Exception:
                pass

        # ---- viz: vial_overlay (三线叠加) ----
        try:
            base = getattr(ctx.tube_state, 'viz', None)
            last_raw = ctx.li_state.get('last_raw') if ctx.li_state else None
            vis = _viz.make_vial_overlay_locked(
                base, frame,
                scale_line_x=scale_line_x,
                locked_y0=ctx.locked_y0,
                locked_y1=ctx.locked_y1,
                last_raw=last_raw,
                l2s=l2s,
                label=None,
            )
            dbg_stream.push('vial_overlay', vis)
        except Exception:
            pass

        # ---- viz: tube_edges_binary ----
        try:
            tube_edges = _viz.make_tube_edges_viz(frame, ctx.tube_state)
            if tube_edges is not None:
                dbg_stream.push('tube_edges_binary', tube_edges)
        except Exception:
            pass

        # ---- viz: liquid_signal_panel ([E2] sobel_proj 1D 信号链) ----
        try:
            if signals is not None:
                panel = _viz.make_liquid_signal_panel(
                    frame, signals,
                    peak_strength=ctx.li_state.get('last_peak_strength') if ctx.li_state else None,
                    decision=ctx.li_state.get('last_decision') if ctx.li_state else None,
                    y0=ctx.locked_y0,
                    y1=ctx.locked_y1,
                    raw_history=list(ctx.li_state.get('raw_history', [])) if ctx.li_state else None,
                    last_raw=ctx.li_state.get('last_raw') if ctx.li_state else None,
                )
                if panel is not None:
                    dbg_stream.push('liquid_signal_panel', panel)
        except Exception:
            pass

        # ---- 4/5) 事件 ----
        # is_reached 判定嵌套在 (liquid_entered AND li_state_pinned!=None) 内, 然后比 LIQ_REACH_THR_PX.
        events: List[Event] = [LiquidUpdated(l2s=l2s, peak_strength=float(peak or 0.0))]
        is_reached = False
        if ctx.liquid_entered and li_state_pinned is not None:
            if l2s <= LIQ_REACH_THR_PX:
                is_reached = True
        if is_reached:
            events.append(CompletionConfirmed())
            ctx.l2s = 0
        return events
