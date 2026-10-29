# -*- coding: utf-8 -*-
# =============================================================================
# framework —— auto_volume_control 主流程
# -----------------------------------------------------------------------------
# 拆分:
#   - Phase / Event / StateMachine + TRANSITIONS  (流程模型)
#   - PhaseHandler 子类 × 6                       (每个 phase 一个文件; 每帧计算)
#   - Action × 9                                  (跨相副作用; 集中在 actions.py)
#   - Adapter × 3 (Camera, UpperLink, VizRecorder)(资源/IO 封装)
#   - algo/tree.py                                (POST-MERGE 共享 trunk gray_full)
#   - Pipeline                                    (主循环编排)
#
# 入口: ../main.py
#
# 模块依赖 DAG (无环):
#   phase                    ← 基类 (依赖 stdlib only)
#   event                    ← 基类
#   algo/tree                ← 依赖 cv2 + numpy
#   viz_recorder             ← 依赖 stdlib + subprocess (ffmpeg)
#   camera                   ← 依赖 rt_config + camera_io (lazy)
#   upper_link               ← 依赖 rt_util + control_channel + .event + .phase
#   actions                  ← 依赖 .event + .phase + algo modules + (lazy phases.lock_scale)
#   state_machine            ← 依赖 .event + .phase + (re-export) actions
#   phases/*                 ← 依赖 .event + .phase + algo modules + state_machine 的常量
#   pipeline                 ← 依赖 algo + .phase + state_machine + upper_link
# =============================================================================
from .phase import Phase, PhaseContext, PhaseHandler, ACTIVE, TERMINAL
from .event import (
    CmdPause, CmdStart, CmdStop,
    CompletionConfirmed, Event, ExposureDone,
    LiquidUpdated, RecheckFailed, ScaleScored,
    TubeDetected, TubeLost,
)
from .state_machine import (
    ACTIONS, GUARDS, LIQ_REACH_THR_PX, Row,
    StateMachine, TRANSITIONS,
)
from .pipeline import Pipeline, StubPhaseHandler, make_stub_handlers
from .camera import Camera
from .upper_link import UpperLink, to_wire_status
from .viz_recorder import VizRecorder
from .phases import (
    CompletedPhase, DosingPhase, IdlePhase, LockScalePhase,
    RecheckFailPhase, SearchTubePhase, SKIP_TUBE_HEIGHT_MIN,
    make_handlers,
)

__all__ = [
    # ==== phase 模型 (phase.py) ====
    "Phase",                # IntEnum: IDLE/SEARCH_TUBE/LOCK_SCALE/DOSING/COMPLETED/RECHECK_FAIL
    "PhaseContext",         # 跨 Phase 共享数据 + service refs
    "PhaseHandler",         # 抽象基类
    "ACTIVE", "TERMINAL",   # Phase 集合常量, 给 TRANSITIONS 表批量构造用

    # ==== 事件 (event.py) ====
    "Event",                                                          # 根基类
    "CmdStart", "CmdPause", "CmdStop",                                # 上位机命令
    "TubeDetected", "TubeLost", "ExposureDone", "ScaleScored",        # sensor 事件
    "LiquidUpdated", "CompletionConfirmed", "RecheckFailed",

    # ==== 状态机 (state_machine.py + actions.py) ====
    "Row",                  # transition 规则单条
    "StateMachine",         # 表分发器
    "TRANSITIONS",          # 21 行 Row 列表
    "ACTIONS",              # 9 个 action 字典 (re-export from actions.py)
    "GUARDS",               # 3 个 guard 字典
    "LIQ_REACH_THR_PX",     # 唯一框架级算法常量 (=18)

    # ==== 主循环 (pipeline.py) ====
    "Pipeline",
    "StubPhaseHandler", "make_stub_handlers",   # 占位 handler, framework-only smoke 用

    # ==== Adapters ====
    "Camera",               # camera.py — camera_io 上下文管理器
    "UpperLink",            # upper_link.py — TX/RX/heartbeat 适配器
    "to_wire_status",       # upper_link.py — Phase + run_state → STATUS_* 字节映射
    "VizRecorder",           # viz_recorder.py — viz 流落盘 + H.264 收尾压缩

    # ==== 6 个 PhaseHandler 子类 (phases/) ====
    "IdlePhase", "SearchTubePhase", "LockScalePhase",
    "DosingPhase", "CompletedPhase", "RecheckFailPhase",
    "make_handlers",        # 一键构造 6-handler dict, 给 Pipeline 用
    "SKIP_TUBE_HEIGHT_MIN", # SearchTubePhase 高度门限常量
]
