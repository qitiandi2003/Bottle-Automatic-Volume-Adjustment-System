# -*- coding: utf-8 -*-
# =============================================================================
# framework/actions.py —— StateMachine Action 实现
# -----------------------------------------------------------------------------
# 9 个 transition action. 全部签名 (ctx: PhaseContext, event: Event) -> None.
# 服务引用 (upper_link / viz_recorder / cam / logger) 都从 ctx 取 — Pipeline 在 ctx
# 上注入, actions 直接读, 不走 closure.
#
# 协议字节处理约定:
#   - StartSession                 : 发 INIT (force=True)
#   - EndSession{Paused,Stopped}   : 发 PAUSED/STOPPED (force=True). UpperLink._on_cmd 已把 run_state 设 IDLE.
#   - EmitCompletionPacket         : 发 COMPLETED (force=True), 然后 *自己* 把 run_state 设 IDLE
#                                    让后续 send 因 is_running 闸门而不再发包.
#   - EmitRecheckFailPacket        : 发 RECHECK_FAIL (force=True), 同上.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict

import cv2
import numpy as np

from bottle_volume_control.camera import exposure as auto_exposure
from bottle_volume_control.vision import scale as center_scale
from bottle_volume_control.vision import stability as gap_stability
from bottle_volume_control.vision import liquid as li_det
from bottle_volume_control import config as rt_config
from bottle_volume_control import utils as rt_util
from bottle_volume_control.diagnostics import stream as dbg_stream
from bottle_volume_control.control.channel import STATE_IDLE

from .events import (
    CmdPause, CmdStart, CmdStop, CompletionConfirmed, Event,
    LiquidUpdated, RecheckFailed, ScaleScored,
)
from .context import PhaseContext
# init_score_base_state 在 _action_start_session 里 lazy import — 避免
# state_machine.py → actions.py → phases/__init__.py → phases/dosing.py →
# state_machine.LIQ_REACH_THR_PX 的循环导入.


# ===========================================================================
#                              日志小工具
# ===========================================================================

def _info(ctx: PhaseContext, msg: str) -> None:
    if ctx.logger is not None:
        try: ctx.logger.info(msg); return
        except Exception: pass
    print(msg)


def _err(ctx: PhaseContext, msg: str) -> None:
    if ctx.logger is not None:
        try: ctx.logger.error(msg); return
        except Exception: pass
    print(msg)


# ===========================================================================
#                              Action 实现
# ===========================================================================

def _action_start_session(ctx: PhaseContext, event: Event) -> None:
    """CMD_START 收到 → 重置全部检测状态 + 重置曝光 + 开 VizRecorder session + 发 INIT 包."""
    if not isinstance(event, CmdStart):
        return

    ctx.run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    ctx.frame_index = 0
    ctx.exposure_done = False

    ctx.last_cmd_height = event.height
    ctx.last_cmd_scale_height = event.scale_height
    ctx.last_cmd_volume = event.volume
    ctx.last_cmd_pause = False

    ctx.last_tube_ok = False
    ctx.tube_state = None
    ctx.scale_line_x = None
    ctx.locked_res = None
    ctx.locked_y0 = None
    ctx.locked_y1 = None
    ctx.locked_detection_start_x = None
    ctx.first_frame_brightness = None
    ctx.gap = None
    ctx.l2s = None

    ctx.li_state = li_det.reset_liquid_state()
    ctx.liquid_entered = False
    from .phases.lock_scale import init_score_base_state  # lazy: 避免循环导入
    init_score_base_state(ctx)

    # dosing_first_frame: FreezeScale action 切相到 DOSING 时会再置 True; StartSession 这里清一遍是
    # 防御性 (CmdStart 从 COMPLETED/RECHECK_FAIL 进入时 ctx 残留旧值).
    ctx.dosing_first_frame = False
    ctx.tube_state_fresh = False
    # CMD_START 重發 (上位機判失敗后回零重試, 期間不發 PAUSE) → 重置 auto-exposure flag.
    # 每次 START 重新進 SEARCH_TUBE → 試管 ok 後重跑 RunAutoExposureOnce action.
    ctx.exposure_done = False

    # session-scoped 跟踪器
    ctx.tube_tracker = center_scale.SlewTubeTracker(
        update_every=3, y_low=0.30, y_high=0.75,
        left_inc_max=10, right_dec_max=10, margin=3,
    )
    ctx.gap_stab = gap_stability.GapStability(
        window_s=10.0, span_thr_px=15, min_samples=3,
    )

    # 复位 boot 曝光
    cam = ctx.cam
    if cam is not None and rt_config.EXP_BOOT_ENABLED:
        try:
            cam.MV_CC_SetEnumValue("ExposureAuto", 0)
            cam.MV_CC_SetFloatValue("ExposureTime", float(rt_config.EXP_BOOT_US))
            _info(ctx, f"[action] StartSession: exposure -> manual {rt_config.EXP_BOOT_US:.0f}us")
        except Exception as e:
            _err(ctx, f"[action] StartSession: exposure reset failed: {e}")

    if ctx.viz_recorder is not None:
        try:
            ctx.viz_recorder.open_run(ctx.run_id)
        except Exception as e:
            _err(ctx, f"[action] StartSession: viz_recorder.open_run failed: {e}")

    # INIT 包 (force=True 让 run_state==IDLE 也能发出去). l2s 必須傳 None (→ wire 10000 占位),
    # 不能是 0 — l2s=0 在協議上代表"液面已到刻度", 上位機會誤觸發.
    if ctx.upper_link is not None:
        ctx.upper_link.send(rt_util.STATUS_INIT, ctx.gap, None, force=True)

    _info(ctx, f"[action] StartSession: run_id={ctx.run_id} cmd_height={event.height}")


def _action_end_session_paused(ctx: PhaseContext, event: Event) -> None:
    """CMD_PAUSE 收到 → PAUSED 包 + 关 VizRecorder session.
    UpperLink._on_cmd 已经把 run_state 设了 IDLE. 这里只发包 + 标 last_cmd_pause=True.
    l2s 必須是 None (→ wire 10000), 不能是 0 (= 已到刻度信号).
    """
    ctx.last_cmd_pause = True
    if ctx.upper_link is not None:
        ctx.upper_link.send(rt_util.STATUS_PAUSED, ctx.gap, None, force=True)
    if ctx.viz_recorder is not None:
        try: ctx.viz_recorder.close_run()
        except Exception: pass
    _info(ctx, "[action] EndSessionPaused")


def _action_end_session_stopped(ctx: PhaseContext, event: Event) -> None:
    """CMD_STOP 收到 → STOPPED 包 + 关 VizRecorder session. l2s=None 同 PAUSED."""
    ctx.last_cmd_pause = False
    if ctx.upper_link is not None:
        ctx.upper_link.send(rt_util.STATUS_STOPPED, ctx.gap, None, force=True)
    if ctx.viz_recorder is not None:
        try: ctx.viz_recorder.close_run()
        except Exception: pass
    _info(ctx, "[action] EndSessionStopped")


def _action_run_auto_exposure_once(ctx: PhaseContext, event: Event) -> None:
    """SEARCH_TUBE 第一次见到试管时跑一次自动曝光.

    Idempotent: TRANSITIONS 把这个 action 放在 SEARCH_TUBE→LOCK_SCALE row 上, 第一次 TubeDetected 跑;
    若被重复路径触达 (Cmd 重置后又一次 TubeDetected), ctx.exposure_done=True 时直接 noop.
    """
    if ctx.exposure_done:
        return
    cam = ctx.cam
    bgr = ctx.current_bgr_frame
    st = ctx.tube_state
    if cam is None or bgr is None or st is None or not getattr(st, 'ok', False):
        # 缺料 (Orin 镜像无 cam, 或调度顺序意外) 也直接 mark done — 否则 SM 每帧重跑曝光
        ctx.exposure_done = True
        _err(ctx, "[action] RunAutoExposureOnce: missing cam/bgr/tube_state; marking done to avoid stall")
        return
    # POST-MERGE [B]: 透共享 gray_full, 省掉 _safe_gray(roi2) 的二次 cvtColor.
    gray_full = ctx.tree.gray_full if ctx.tree is not None else None
    try:
        out = auto_exposure.auto_adjust_exposure_once(
            cam, bgr, st, tube_type='auto',
            first_base_us=float(rt_config.EXP_BOOT_US),
            gray_full=gray_full,
        )
        _info(ctx,
              f"[action] auto-expo: ok={out.get('ok')} "
              f"prev={out.get('prev_exp')} new={out.get('new_exp')} "
              f"median={out.get('median')} type={out.get('tube_type')}")
    except Exception as e:
        _err(ctx, f"[action] auto_adjust_exposure_once raised: {e}")
    ctx.exposure_done = True


def _action_freeze_scale_and_calibrate(ctx: PhaseContext, event: Event) -> None:
    """LOCK_SCALE → DOSING 跨相 action. 仅做 refine + 几何缓存 + reset li_state.

    **不**做 calibrate_locked_frame, **不**抓 first_frame_brightness — 这两件事推迟到 DOSING 第一帧
    (DosingPhase 看到 ctx.dosing_first_frame=True 时执行).

    步骤:
      1) refine_scale_edges 取右沿做亚像素锁定 (rt_config.SCALE_REFINE_*) [可关]
      2) 计算 gap = -(x_lock - cx); 缓存 locked_y0/y1/detection_start_x
      3) reset li_state (新一轮液面跟踪)
      4) 设 ctx.dosing_first_frame=True — DosingPhase 第一帧据此决定跑 calibrate

    名字保留 "freeze_scale_and_calibrate" 减少 churn; 实际语义现在是 "freeze scale only".
    """
    if not isinstance(event, ScaleScored):
        return
    bgr = ctx.current_bgr_frame
    st = ctx.tube_state
    if bgr is None or st is None or not getattr(st, 'ok', False):
        _err(ctx, "[action] FreezeScale: missing bgr/tube_state; abort transition")
        return

    cx = bgr.shape[1] // 2
    x_lock = int(event.line_x)

    # POST-MERGE: 共享 gray_full ([D] refine; li_det 算法不动所以不取 G_full)
    gray_full = ctx.tree.gray_full if ctx.tree is not None else None

    # ---- 1. refine [D] ----
    if rt_config.SCALE_REFINE_ENABLED:
        try:
            refine_out = center_scale.refine_scale_edges(
                bgr, st, x_lock,
                band=rt_config.SCALE_REFINE_BAND,
                use_mask=rt_config.SCALE_REFINE_USE_MASK,
                smooth_ksize=rt_config.SCALE_REFINE_SMOOTH,
                make_debug=True,
                gray_full=gray_full,
            )
        except Exception as e:
            _err(ctx, f"[action] refine_scale_edges raised: {e}")
            refine_out = None
        if refine_out and refine_out.get('ok'):
            new_x = refine_out.get('x_right')
            if new_x is not None:
                _info(ctx,
                      f"[action] FreezeScale refine: x {x_lock}->{int(new_x)} "
                      f"width={refine_out.get('width')}")
                x_lock = int(new_x)
            try:
                profile = refine_out.get('profile')
                if profile is not None:
                    dbg_stream.push('scale_refine_profile', profile)
            except Exception:
                pass

    # ---- 2. 几何缓存 ----
    locked_y0 = int(getattr(st, 'y0', 0))
    locked_y1 = int(getattr(st, 'y1', 0))
    x_right = int(getattr(st, 'x_right', bgr.shape[1] - 1))
    locked_detection_start_x = max(x_lock + 100, x_right - 50)

    ctx.scale_line_x = x_lock
    ctx.gap = int(-(x_lock - cx))
    # locked_res 留给 DOSING 第一帧 calibrate 来填.
    ctx.locked_res = None
    ctx.locked_y0 = locked_y0
    ctx.locked_y1 = locked_y1
    ctx.locked_detection_start_x = locked_detection_start_x

    # ---- 3. reset li_state ----
    ctx.li_state = li_det.reset_liquid_state()

    # ---- 4. mark first frame in DOSING ----
    ctx.dosing_first_frame = True
    # first_frame_brightness 也在 DOSING 第一帧才抓; 这里清旧值防残留.
    ctx.first_frame_brightness = None

    _info(ctx,
          f"[action] FreezeScale: x_lock={x_lock} gap={ctx.gap} "
          f"y0/y1={locked_y0}/{locked_y1} det_start_x={locked_detection_start_x} "
          f"(calibrate deferred to DOSING first frame)")


def _action_clear_scale_state(ctx: PhaseContext, event: Event) -> None:
    """LOCK_SCALE → SEARCH_TUBE 跨相 (TubeLost). 清掉刻度相关缓存 + 复位 gap_stab."""
    ctx.scale_line_x = None
    ctx.locked_res = None
    ctx.locked_y0 = None
    ctx.locked_y1 = None
    ctx.locked_detection_start_x = None
    if ctx.gap_stab is not None:
        try: ctx.gap_stab.reset()
        except Exception: pass
    _info(ctx, "[action] ClearScaleState (LOCK_SCALE -> SEARCH_TUBE on TubeLost)")


def _action_clear_liquid_state(ctx: PhaseContext, event: Event) -> None:
    """DOSING → SEARCH_TUBE 跨相 (TubeLost). 清液面状态 + 锁定缓存."""
    ctx.li_state = li_det.reset_liquid_state()
    ctx.scale_line_x = None
    ctx.locked_res = None
    ctx.locked_y0 = None
    ctx.locked_y1 = None
    ctx.locked_detection_start_x = None
    ctx.first_frame_brightness = None
    if ctx.gap_stab is not None:
        try: ctx.gap_stab.reset()
        except Exception: pass
    _info(ctx, "[action] ClearLiquidState (DOSING -> SEARCH_TUBE on TubeLost)")


def _action_emit_completion_packet(ctx: PhaseContext, event: Event) -> None:
    """DOSING → COMPLETED 跨相 (CompletionConfirmed).

    关键: 发 COMPLETED 后, *主动* 把 run_state 设 IDLE — 让 Pipeline 后续每帧
    upper_link.send 因 'is_running()=False && !force' 闸门而不再发包.

    wire 字段:
      gap=None (→ wire 10000 占位), 不传 ctx.gap. COMPLETED 包语义是"任务完成", 不带实时刻度偏移;
      上位机看到 gap=10000 即知此包是终态信号.
      l2s=0 → 协议层 "液面已到刻度" 信号.
    """
    if ctx.upper_link is not None:
        ctx.upper_link.send(rt_util.STATUS_COMPLETED, None, 0, force=True)
        try:
            ctx.upper_link.run_state.set(STATE_IDLE)
        except Exception:
            pass
    if ctx.viz_recorder is not None:
        try: ctx.viz_recorder.close_run()
        except Exception: pass
    _info(ctx, "[action] EmitCompletionPacket")


def _action_emit_recheck_fail_packet(ctx: PhaseContext, event: Event) -> None:
    """DOSING → RECHECK_FAIL 跨相 (RecheckFailed).

    当前没有 PhaseHandler emit RecheckFailed 事件; RECHECK_FAIL 槽 + Action 留作未来接入复核逻辑用.
    """
    if ctx.upper_link is not None:
        ctx.upper_link.send(rt_util.STATUS_RECHECK_FAIL, ctx.gap, 10000, force=True)
        try:
            ctx.upper_link.run_state.set(STATE_IDLE)
        except Exception:
            pass
    if ctx.viz_recorder is not None:
        try: ctx.viz_recorder.close_run()
        except Exception: pass
    _info(ctx, "[action] EmitRecheckFailPacket")


# ===========================================================================
#                              ACTIONS dict
# ===========================================================================

ACTIONS: Dict[str, Callable[[PhaseContext, Event], None]] = {
    "StartSession":            _action_start_session,
    "EndSessionPaused":        _action_end_session_paused,
    "EndSessionStopped":       _action_end_session_stopped,
    "RunAutoExposureOnce":     _action_run_auto_exposure_once,
    "FreezeScaleAndCalibrate": _action_freeze_scale_and_calibrate,
    "ClearScaleState":         _action_clear_scale_state,
    "ClearLiquidState":        _action_clear_liquid_state,
    "EmitCompletionPacket":    _action_emit_completion_packet,
    "EmitRecheckFailPacket":   _action_emit_recheck_fail_packet,
}
