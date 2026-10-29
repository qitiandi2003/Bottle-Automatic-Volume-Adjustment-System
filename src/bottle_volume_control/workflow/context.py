# -*- coding: utf-8 -*-
# =============================================================================
# framework/phase.py —— Phase 枚举 + PhaseHandler ABC + PhaseContext
# -----------------------------------------------------------------------------
# 三个对象的关系:
#   Phase            —— 6 状态枚举值, IntEnum. 内部 phase 和 wire-protocol status 解耦;
#                        映射在 upper_link.to_wire_status.
#   PhaseContext     —— 跨 Phase 的可变共享状态 (li_state / scale_line_x / gap / l2s ...).
#                        所有 PhaseHandler / Action / Guard 都通过它读写状态;
#                        匹配 "dict-state + 纯函数" 的项目原则.
#   PhaseHandler     —— 抽象基类, 子类是每个 Phase 一个 (6 个), 负责"当前 Phase 的
#                        每帧计算" → 返回 sensor events 给 StateMachine.
#                        子类实现见 framework/phases/.
# =============================================================================
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    import numpy as np
    from .events import Event


class Phase(IntEnum):
    """内部 Phase 枚举. 与 wire-protocol STATUS_* 解耦; 映射在 upper_link.to_wire_status."""
    IDLE         = 0   # 等 CmdStart
    SEARCH_TUBE  = 1   # 试管检测 + 一次性自动曝光
    LOCK_SCALE   = 2   # 找刻度中线 (find_center_scale_line); SCORE_BASE 采樣→freeze p95×2 → score 過閾 + gap_stab 穩 才切
    DOSING       = 3   # 锁定后逐帧液面跟踪 (update_liquid_level)
    COMPLETED    = 4   # 终态; 等下一次 CmdStart 或 CmdStop
    RECHECK_FAIL = 5   # 终态 (复核失败)


# 用于 transition 表批量构造 (CmdStart/Pause/Stop 在多个 Phase 上行为一致)
TERMINAL: tuple = (Phase.COMPLETED, Phase.RECHECK_FAIL)
ACTIVE:   tuple = (Phase.SEARCH_TUBE, Phase.LOCK_SCALE, Phase.DOSING)


@dataclass
class PhaseContext:
    """跨 Phase 的共享可变状态 + service refs. 由 Pipeline 拥有, 注入到 handler/action/guard.

    服务引用 (cam/upper_link/viz_recorder/logger) 放这里有点违反"纯数据"直觉, 但比另一套
    DI 容器简单很多: actions 和 handlers 直接 ctx.upper_link.send(...) 即可, 不用 closure 也不用 self.
    项目硬约束: 不为 dict+function 套额外类层 (TODO.md Refactor design rules > State representation).

    fields 分组:
      [service refs] cam / upper_link / viz_recorder / logger — Pipeline.__init__ 注入
      [session]      run_id / frame_index / exposure_done / last_cmd_*  — StartSession 写, EndSession 清
      [tube]         last_tube_ok / tube_state / tube_tracker
      [scale lock]   scale_line_x / locked_res / locked_y0/y1 / locked_detection_start_x / first_frame_brightness
      [algo state]   li_state / gap_stab
      [per-frame]    current_bgr_frame — Pipeline 每帧塞值, actions 读取
      [protocol]     gap / l2s — UpperLink 发包时读
    """

    # ---- service refs (Pipeline 注入) ----
    cam: Any = None
    upper_link: Any = None
    viz_recorder: Any = None
    logger: Any = None

    # ---- 会话级 (StartSession 写入, EndSession 清空) ----
    run_id: Optional[str] = None
    frame_index: int = 0
    exposure_done: bool = False

    # ---- Cmd 来源信息 ----
    last_cmd_height: Optional[float] = None
    last_cmd_scale_height: Optional[float] = None
    last_cmd_volume: Optional[float] = None
    last_cmd_pause: bool = False

    # ---- 试管几何 ----
    last_tube_ok: bool = False
    tube_state: Any = None       # center_scale.TubeState | None
    tube_tracker: Any = None     # center_scale.SlewTubeTracker | None (StartSession 创建)
    # 同帧 SEARCH_TUBE → LOCK_SCALE extra-handle 时的 tracker 复用标志.
    # SearchTubePhase / LockScalePhase 哪个先跑 update 就置 True; 同帧另一个 phase 看到 True 跳过自己
    # 的 update 直接读 ctx.tube_state. Pipeline.run() 每帧顶部重置 False. 保证"每帧只跑一次 tracker.update"
    # 不让 SlewTubeTracker._frame_count 同帧多次 ++.
    tube_state_fresh: bool = False

    # ---- 锁定/校准缓存 (FreezeScaleAndCalibrate 写入) ----
    scale_line_x: Optional[int] = None
    locked_res: Any = None       # li_det.calibrate_locked_frame 返回的整个 dict
    locked_y0: Optional[int] = None
    locked_y1: Optional[int] = None
    locked_detection_start_x: Optional[int] = None
    first_frame_brightness: Optional[float] = None  # DOSING 第一帧的 gray-median; 之后帧用作维持 baseline
    # DOSING 第一帧标志. FreezeScale action 在 LOCK_SCALE→DOSING 跨相时置 True;
    # DosingPhase.handle 在 first_frame 跑 calibrate_locked_frame + 抓 brightness baseline,
    # 不跑 update_liquid_level. 之后由 DosingPhase 自己设回 False.
    dosing_first_frame: bool = False

    # ---- 算法状态 ----
    li_state: Any = None         # li_det.reset_liquid_state() 返回的 dict
    gap_stab: Any = None         # gap_stability.GapStability instance (StartSession 创建)
    # liquid_entered 粘性 True — 某幀 update_liquid_level 返回 pinned!=None 就置 True, 之後不會回 False
    # (StartSession / DosingPhase first_frame 才 reset). l2s 计算条件: liquid_entered AND
    # li_state['pinned_max_x'] is not None → l2s = pinned - scale_line_x; 否则 l2s = 10000.
    liquid_entered: bool = False

    # ---- SCORE_BASE 自适应阈值状态 ----
    # LockScalePhase 在 height >= ctx.last_cmd_scale_height (上位機協議 "預設容量瓶刻度線高度") 時累計
    # score 樣本; 採滿 SCORE_BASE_SAMPLES_MAXLEN (=10) 個樣本同幀 freeze: score_base_value=p95(samples),
    # threshold=value*2. Freeze 之前 _score_threshold 兜底 10000 (score 永不過閾, 不鎖). 採樣 path 一律
    # return — 採樣中 wire gap 恆 None (=10000 placeholder), 防實時 gap 污染.
    score_base_samples: Any = None       # collections.deque(maxlen=10); StartSession 创建
    score_base_value: Optional[float] = None
    score_base_ready: bool = False

    # ---- 当前帧 (Pipeline 每帧设置, 供 actions/guards 读取) ----
    current_bgr_frame: Any = None
    # POST-MERGE 共享 trunk (gray_full / G_full); algo.tree.prepare_frame 构造.
    # Pipeline.run() 在 handler.handle 之前一次性算好; 下一帧整个换新实例.
    tree: Any = None

    # ---- 协议字段 ----
    gap: Optional[int] = None    # scale_line_x - W/2 (px); UpperLink.send(gap_px=...)
    l2s: Optional[int] = None    # liquid_max_x - scale_line_x (px); 10000=invalid

    # ---- 调度元信息 ----
    last_status_emit_ts: float = 0.0


class PhaseHandler(ABC):
    """每个 Phase 的每帧计算入口.

    契约:
        - phase 类属性指明该 handler 所属 Phase (Pipeline dispatch 时校验).
        - handle(frame, ctx) 返回 sensor Event 列表 (可空); Pipeline 把这些
          events 餵给 StateMachine, SM 依据 transition 表决定是否切 Phase.
        - on_enter / on_exit 为可选钩子 (默认 no-op); StateMachine 切相时调用.
        - 子类不应保存跨 Phase 的状态; 用 PhaseContext.

    子类必须使用 @final 标注或在 phase 处显式赋值, 例如:
        class IdlePhase(PhaseHandler):
            phase = Phase.IDLE
            def handle(self, frame, ctx): return []
    """

    phase: Phase  # 子类必填; ABC 不强校验, 但 Pipeline 启动时会 assert

    @abstractmethod
    def handle(self, frame: "np.ndarray", ctx: PhaseContext) -> "List[Event]":
        """处理一帧, 返回触发的 sensor events (顺序保留)."""

    def on_enter(self, ctx: PhaseContext) -> None:
        """进入此 Phase 的钩子 (默认 no-op)."""
        return None

    def on_exit(self, ctx: PhaseContext) -> None:
        """离开此 Phase 的钩子 (默认 no-op)."""
        return None
