# -*- coding: utf-8 -*-
# =============================================================================
# framework/event.py —— StateMachine 事件类型 (frozen dataclass)
# -----------------------------------------------------------------------------
# StateMachine 是表驱动 + 事件触发的, 这里定义所有事件类型.
#
# 两类事件 (按来源分):
#   Cmd*       —— 上位机命令. 来源 = UpperLink.ControlReader 解码 15B TCP 帧后 emit.
#                  跨线程: Reader daemon 线程 put 到 cmd_queue, 主线程每帧 drain.
#                  载荷 = height / scale_height / volume. scale_height 用作 SCORE_BASE 采样触发
#                  (LockScalePhase: height >= scale_height); volume 仍是协议占位符.
#   Sensor*    —— PhaseHandler.handle() 返回值. 描述"本帧的算法观察".
#                  同线程 emit (主线程), 立即被 SM.feed_many 消费.
#
# 设计约束:
#   - 全部 frozen=True : 事件构造后不可改. 避免 handler/action 中悄悄修改影响后续 dispatch.
#   - 全部继承 Event   : type-tag 作用 (Row.event 是 class, dispatch 用 isinstance 匹配).
#                       不依赖类继承层次 — 所有事件类都直接继承 Event 基类, 没多层嵌套.
#   - 无方法           : 事件是 *数据*, 处理逻辑在 PhaseHandler / actions / guards.
#
# 命名约定:
#   - Cmd*       : 来自上位机 (外部输入). UpperLink._on_cmd 创建.
#   - Sensor*    : 算法侧观察. PhaseHandler.handle() 创建.
#   - 过去式动词 : Detected / Lost / Confirmed / Failed / Updated / Scored — 描述"已发生的事实",
#                 不是命令也不是请求.
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Event:
    """所有事件的根类型 (空基类).

    SM dispatch 用 isinstance(event, row.event) 做表查匹配; 这里是 type tag, 没字段.
    isinstance 而不是 type==: 万一未来某个事件加了细分子类 (如 TubeDetected → TubeDetectedFront/Back),
    旧 transition 行还能命中父类规则. 当前所有事件都直接继承 Event, 没用到这个特性.
    """
    pass


# ====================================================================
#                              Cmd 事件 (3)
# ====================================================================

@dataclass(frozen=True)
class CmdStart(Event):
    """上位机 START 命令.

    携带这一轮注液的目标参数:
        height       : 当前机械臂高度 (mm). 用于 SCORE_BASE 高度门控 (lock_scale.py).
        scale_height : 目标刻度对应的高度. 协议占位字段, 算法当前不消费.
        volume       : 目标体积 (mL). 协议占位字段, 算法当前不消费.

    StartSession action 收到后:
      - 重置 ctx 全部业务状态 (li_state / tube_tracker / score_base / etc)
      - 复位相机曝光到 EXP_BOOT_US
      - 开 VizRecorder session (per-run-id 子目录, 接收 debug_stream tee 的 JPEG)
      - 发 INIT 包 (force=True, 让上位机立即看到设备就绪)
      - 切 phase 到 SEARCH_TUBE
    """
    height: Optional[float] = None
    scale_height: Optional[float] = None
    volume: Optional[float] = None


@dataclass(frozen=True)
class CmdPause(Event):
    """上位机 PAUSE. 行为 = STOP, 但 to_wire_status 输出 PAUSED (4) 而不是 STOPPED (5).

    意义对上位机: PAUSED 暗示"任务还在, 可以 START 续做"; STOPPED = "任务结束".
    本系统内部不区分 — 都是 EndSession action + 切 IDLE phase. 区别只体现在 wire status 字节.
    """
    pass


@dataclass(frozen=True)
class CmdStop(Event):
    """上位机 STOP. 同 CmdPause, 但 wire status 输出 STOPPED."""
    pass


# ====================================================================
#                            Sensor 事件 (7)
# ====================================================================

@dataclass(frozen=True)
class TubeDetected(Event):
    """SearchTube/LockScale 本帧 SlewTubeTracker.update 命中试管.

    tube_state: center_scale.TubeState 实例 (含 y0/y1/x_left/x_right/mask/viz/edges_*).
    实际上 handler 已经把 tube_state 写到 ctx.tube_state, 这里 event 携带是冗余 — 但保留是为了
    SM dispatch + action 路径里如果需要可以从 event 直取 (避免依赖 ctx 时序).
    """
    tube_state: Any = None


@dataclass(frozen=True)
class TubeLost(Event):
    """本帧没检测到试管 (Hough 失败 / center 不在 tube 内 / height < SKIP_TUBE).

    在 SearchTube → SearchTube self-loop (无 action; wire status 转 NO_TUBE).
    在 LockScale → SearchTube (action ClearScaleState 清刻度缓存).
    在 Dosing → SearchTube (action ClearLiquidState 清液面状态).
    """
    pass


@dataclass(frozen=True)
class ExposureDone(Event):
    """auto_adjust_exposure_once 完成. 当前 *不被使用* — 曝光逻辑直接通过 ctx.exposure_done bool 标志.

    保留是为了未来可能扩展 (e.g. 异步曝光 → ExposureDone event 通知). RunAutoExposureOnce action
    内部直接设 ctx.exposure_done=True, 没 emit 这个 event.
    """
    pass


@dataclass(frozen=True)
class ScaleScored(Event):
    """LockScale 本帧产出一个候选刻度位 + 强度.

    LockScalePhase.handle() 内部已经包了 SCORE_BASE 自适应 + gap_stab 稳定性判断;
    只有"通过全部条件"时才会 emit 这个事件. SM 看到事件即转 DOSING (无 score 守卫).

    score : 找到的列能量峰值 (浮点). >SCORE_BASE_THR_MULTIPLIER × score_base_value 时才会 emit.
    line_x: 像素 x 坐标 (整数). FreezeScaleAndCalibrate action 用它做 refine_scale_edges 的种子点.
    """
    score: float
    line_x: int


@dataclass(frozen=True)
class LiquidUpdated(Event):
    """Dosing 本帧液面更新.

    DosingPhase.handle 每帧 emit (即使 raw 信号弱也 emit, 携带 l2s=10000 占位).
    SM dispatch 时:
      - 若 l2s > LIQ_REACH_THR_PX (=18) → guard L2sAboveReachThr 命中, DOSING self-loop, 无 action.
      - 若 l2s ≤ 18 → guard 不命中, 这条 row 跳过. handler 同帧也会 emit CompletionConfirmed
                      (DosingPhase.handle 在 reach 时同时 emit 两个事件), 走下一行 → COMPLETED.

    l2s          : 液面到刻度的有符号像素距离 (pinned_max_x - scale_line_x). 越小越接近.
    peak_strength: 当前帧 sobel peak 强度. 用于诊断 (li_signal_panel viz), SM 不消费.
    """
    l2s: int
    peak_strength: float


@dataclass(frozen=True)
class CompletionConfirmed(Event):
    """Dosing 完成判据通过. DosingPhase.handle 在 l2s ≤ LIQ_REACH_THR_PX 时同帧 emit.

    SM 转 COMPLETED, 跑 EmitCompletionPacket action (force-发 STATUS_COMPLETED 包 + run_state.set IDLE).
    """
    pass


@dataclass(frozen=True)
class RecheckFailed(Event):
    """复核失败. 当前 *没有 PhaseHandler 会 emit 它*.

    保留事件类型 + 对应的 action EmitRecheckFailPacket + RECHECK_FAIL phase, 等需要时在
    DosingPhase 里加复核逻辑直接 emit.
    """
    pass
