# -*- coding: utf-8 -*-
# =============================================================================
# framework/state_machine.py —— Row-table 状态机 + TRANSITIONS + GUARDS
# -----------------------------------------------------------------------------
# 模块职责:
#   1) Row + StateMachine    —— 通用的 (state, event_type, next, action, guard)
#                                表分发器. ~50 行实现; 不引入 transitions / pytransitions
#                                等第三方库 (项目硬约束: stdlib + cv2 + numpy + MvImport).
#   2) TRANSITIONS            —— 项目专用 transition 表 (绑死 Phase + Event 类型;
#                                Action / Guard 用字符串名引用, 运行时查 ACTIONS / GUARDS dict).
#   3) GUARDS                 —— 3 个守卫纯函数 (NotYetExposed / ExposureDone /
#                                L2sAboveReachThr); 无副作用, 只读 ctx + event.
#   4) re-export ACTIONS      —— 实际实现在 actions.py; 这里只 from-import.
#
# 为什么不用 transitions 库?
#   - 项目硬约束: 不引入新 pip 依赖.
#   - transitions 库 ~5KLOC, 我们只用 dispatch + table; 写 50 行就覆盖.
#   - 调试可读性: 表是平铺 list, grep 一下就找到所有规则; 库的 hierarchy / parallel
#     /历史等高级特性这里都用不到.
#
# 设计不变量 (后续修改时务必保持):
#   - dispatch 顺序: 当前 phase → event 类型精确匹配 (isinstance) → 评估 guard
#                   → 命中则 fire action + 切 next; 多 row 依列表顺序找第一条满足的.
#   - 多个匹配行如果 guard 重叠, 取第一行 (隐式优先级). 调用方 (TRANSITIONS 表设计者)
#     必须保证多 guard 的逻辑互斥, 否则后续 row 永远不可达.
#   - 未匹配 (state, event_type) = no-op + DEBUG 日志 (不抛异常). 这是容错: handler
#     可能 emit 业务上无意义的事件 (如 IDLE 阶段的 LiquidUpdated), 系统不应崩.
#   - on_enter / on_exit 钩子异常隔离: 一个 handler 的 hook raise 不影响 SM 状态推进.
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

from .event import (
    CmdPause,
    CmdStart,
    CmdStop,
    CompletionConfirmed,
    Event,
    LiquidUpdated,
    RecheckFailed,
    ScaleScored,
    TubeDetected,
    TubeLost,
)
from .phase import ACTIVE, TERMINAL, Phase, PhaseContext


# ===========================================================================
#                          Action / Guard 类型别名
# ===========================================================================
# Action / Guard 都是裸 callable. 字符串名 (用于 TRANSITIONS) 在运行时查 ACTIONS / GUARDS dict.
# 这层"间接索引"的好处: Row 是 frozen dataclass + 可序列化 (字符串名 + Phase + Event 类型);
# 整个表可以打印 / debug 而不暴露闭包对象.
#
# 签名:
#   Action(ctx, event) -> None   - 副作用通过 ctx 持久化, 通过 ctx.upper_link / ctx.viz_recorder
#                                  调外部 IO. 不返回值; 异常被 SM 捕获并 log.
#   Guard(ctx, event) -> bool    - 纯函数, 不应有副作用. False = 这条 Row 不命中, 继续找下一条.
ActionFn = Callable[[PhaseContext, Event], None]
GuardFn = Callable[[PhaseContext, Event], bool]


# ===========================================================================
#                                 Row
# ===========================================================================

@dataclass(frozen=True)
class Row:
    """单条 transition 规则.

    五元组语义:
        start  : 来源 Phase. 当 SM.current == start 时这一行可能命中.
        event  : 事件 *类型* (class), 不是 instance. dispatch 用 isinstance(event, row.event)
                 匹配, 因此子类事件也可匹配父类规则 (但本项目所有事件都直接继承 Event 基类,
                 没有多层继承).
        next   : 目标 Phase. 命中后 SM.current 切到这里.
                 注: 如果 next == start, 这是 self-loop — 不切相, 但 action 会执行.
        action : 副作用名 (str). None = 无副作用. 非 None 时在 ACTIONS dict 里查同名 callable.
                 找不到名字 → 跳过 + warning log (不视为命中失败, 仍切 next).
        guard  : 守卫名 (str). None = 无条件命中 (绝对真).
                 非 None 时在 GUARDS dict 查; guard(ctx, event) 返回 False 视为不命中,
                 SM 继续找下一条 row.

    例:
        Row(IDLE, CmdStart, SEARCH_TUBE, "StartSession", None)
            意思: "在 IDLE 收到 CmdStart, 跑 StartSession action, 切到 SEARCH_TUBE".

        Row(SEARCH_TUBE, TubeDetected, SEARCH_TUBE, "RunAutoExposureOnce", "NotYetExposed"),
        Row(SEARCH_TUBE, TubeDetected, LOCK_SCALE, None, "ExposureDone"),
            两行 same (start, event); guard 互斥分流: 没曝光过 → 跑曝光 stay; 曝光过 → 转 LOCK_SCALE.
    """
    start: Phase
    event: Type[Event]
    next: Phase
    action: Optional[str] = None
    guard: Optional[str] = None


# ===========================================================================
#                              StateMachine
# ===========================================================================

class StateMachine:
    """轻量 Row-table 分发器.

    使用模式 (Pipeline 内部用):
        sm = StateMachine(Phase.IDLE, TRANSITIONS, ACTIONS, GUARDS, logger=lg)
        sm.set_handlers({Phase.IDLE: IdlePhase(), ...})   # 让 SM 在切相时调 on_enter/on_exit
        sm.feed(CmdStart(...), ctx)                        # 单事件
        sm.feed_many([ev1, ev2], ctx)                      # 多事件按顺序
        sm.current                                         # 查当前 Phase

    线程模型: 不是线程安全的. SM 假设全部从单线程 (Pipeline 主循环) 调用.
    Cmd 事件的"跨线程"由 UpperLink.cmd_queue 处理: Reader 线程 put, 主线程 drain 后再 feed SM.
    """

    def __init__(
        self,
        initial: Phase,
        transitions: List[Row],
        actions: Optional[Dict[str, ActionFn]] = None,
        guards: Optional[Dict[str, GuardFn]] = None,
        logger: Any = None,
    ) -> None:
        # 当前 Phase. feed() 切相时直接赋值 (单线程, 无锁).
        self.current: Phase = initial
        # 拷贝构造参数 (防止外部修改影响 SM 内部行为).
        self._transitions: List[Row] = list(transitions)
        self._actions: Dict[str, ActionFn] = dict(actions or {})
        self._guards: Dict[str, GuardFn] = dict(guards or {})
        self._logger = logger
        # PhaseHandler 字典. set_handlers() 注入. 切相时跑 old.on_exit + new.on_enter.
        # 保持空 dict 也能跑 — 此时只是没有 enter/exit 钩子, 但 dispatch 正常.
        self._handlers: Dict[Phase, Any] = {}

    def set_handlers(self, handlers: Dict[Phase, Any]) -> None:
        """注入 PhaseHandler 字典. Pipeline.__init__ 调一次. 之后切相时由 SM 调钩子."""
        self._handlers = dict(handlers)

    # ----------------------------- 事件入口 -----------------------------

    def feed(self, event: Event, ctx: PhaseContext) -> bool:
        """喂入一个事件. 返回 True=命中规则 (无论是否切相), False=未匹配.

        命中处理 (按 row 列表顺序找第一条满足的):
          1. row.start == self.current ?         否 → 跳过本行
          2. isinstance(event, row.event) ?      否 → 跳过本行
          3. row.guard 存在 ?                    存在 → 查 GUARDS, 找不到 = warning + 跳过本行
                                                       查到 → guard(ctx, event) ? False = 跳过本行
          4. row.action 存在 ?                   存在 → 查 ACTIONS, 找不到 = warning + 不跑 action 但仍切 next
                                                       查到 → 调用; 异常被 catch + error log (不阻止切相)
          5. row.next != self.current ?          否 (self-loop) → 直接 return True
                                                 是 → 跑 old.on_exit; 切 self.current; 跑 new.on_enter; 都异常隔离
          6. return True

        未匹配任何 row → 写一条 DEBUG 日志, return False. 不抛异常 — 业务系统应能容忍
        无意义事件 (e.g. IDLE 阶段的 LiquidUpdated 在不该 emit 的代码路径泄漏).
        """
        for row in self._transitions:
            # ---- 步骤 1: phase 匹配 ----
            if row.start != self.current:
                continue
            # ---- 步骤 2: event 类型匹配 (isinstance 允许子类多态, 这里都是直系) ----
            if not isinstance(event, row.event):
                continue
            # ---- 步骤 3: guard 评估 ----
            if row.guard is not None:
                guard_fn = self._guards.get(row.guard)
                if guard_fn is None:
                    # 表里写了 guard 名但 GUARDS dict 没注册 — 配置错误. 跳本行, 让后续 row 还有机会命中.
                    self._log("warning", f"[sm] missing guard '{row.guard}' for {row}")
                    continue
                if not guard_fn(ctx, event):
                    continue
            # ---- 步骤 4: action 触发 (异常吃掉 + log; 不阻止 next 切换) ----
            if row.action is not None:
                action_fn = self._actions.get(row.action)
                if action_fn is None:
                    self._log("warning", f"[sm] missing action '{row.action}' for {row}")
                else:
                    try:
                        action_fn(ctx, event)
                    except Exception as exc:
                        # action 通常做副作用 (发包 / 重置 / 校准); 它崩不能让 SM 卡死.
                        # 切相照旧, 业务后果由日志兜底.
                        self._log("error", f"[sm] action '{row.action}' raised: {exc}")
            # ---- 步骤 5: 切相 + on_exit/on_enter 钩子 ----
            if row.next != self.current:
                old = self._handlers.get(self.current)
                new = self._handlers.get(row.next)
                # 5a) 旧 phase 的 on_exit
                if old is not None:
                    try: old.on_exit(ctx)
                    except Exception as exc:
                        self._log("error", f"[sm] on_exit({self.current.name}) raised: {exc}")
                # 5b) 状态切换日志 (info 级; 用户排查相位流转时主要看这条)
                self._log("info", f"[sm] {self.current.name} -> {row.next.name} via {type(event).__name__}")
                # 5c) 切 current (单语句赋值原子)
                self.current = row.next
                # 5d) 新 phase 的 on_enter
                if new is not None:
                    try: new.on_enter(ctx)
                    except Exception as exc:
                        self._log("error", f"[sm] on_enter({self.current.name}) raised: {exc}")
            # 命中 (无论 self-loop 还是切相)
            return True
        # ---- 全表扫完无命中 ----
        self._log("debug", f"[sm] no rule for ({self.current.name}, {type(event).__name__})")
        return False

    def feed_many(self, events: List[Event], ctx: PhaseContext) -> None:
        """顺序喂多个事件. 注意: 第一个事件可能切了 phase, 后续事件就在新 phase 上 dispatch.

        典型场景: DosingPhase.handle 一帧返回 [LiquidUpdated, CompletionConfirmed]:
          - feed(LiquidUpdated)        → DOSING self-loop (guard L2sAboveReachThr, 但若 reach 已触发 reach<=thr 反而不命中, 走 no-rule debug log)
          - feed(CompletionConfirmed)  → DOSING → COMPLETED (action EmitCompletionPacket)
        """
        for ev in events:
            self.feed(ev, ctx)

    # ----------------------------- 内部工具 -----------------------------

    def _log(self, level: str, msg: str) -> None:
        """统一日志接口. 容错: 没注入 logger 或 logger 没对应 level 方法 → 静默丢."""
        if self._logger is None:
            return
        fn = getattr(self._logger, level, None)
        if callable(fn):
            try: fn(msg)
            except Exception:
                # 日志本身崩不能影响 SM. 兜底吃掉.
                pass


# ===========================================================================
#                          ACTIONS (re-export from actions.py)
# ===========================================================================
# 9 个 Action 的真实实现在 framework/actions.py. 这里只 re-export, 让 state_machine.py
# 保持 dispatcher + 表驱动 + guard 的轻薄角色 (~250 行).
#
# 拆 actions.py 的好处:
#   - 业务副作用 (发包 / 重置 / 校准) 集中一处, 单文件审计;
#   - state_machine.py 不用 import 一堆 algo 模块 (auto_exposure / center_scale / li_det /
#     gap_stability / debug_stream), 减少 import 时间 + 循环依赖风险.
#
# 签名约定: (ctx: PhaseContext, event: Event) -> None
# ===========================================================================
from .actions import ACTIONS  # noqa: F401  re-export


# ===========================================================================
#                              GUARDS
# ===========================================================================
# 3 个守卫. 全是纯函数 (无副作用; 只读 ctx 字段或 event 字段).
# 旧代码里没有"守卫"概念, 所有判定都散在 detector.run() / _process_* 的 if 分支里;
# 状态机抽象后, 这些判定从代码控制流变成数据 (TRANSITIONS 表).
#
# 注: 历史上还有过 ScoreBelowFixedThr / ScoreAboveFixedThr 两个守卫 (SCORE_THR_FIXED 草稿值),
# 后被用户撤回 (要求"算法不改, 只优化框架"). LockScalePhase 现在内部跑 SCORE_BASE 自适应,
# 通过才 emit ScaleScored, 因此 SM 这层无需 score 守卫.
# ===========================================================================

# 算法常量 — 项目硬约束: 算法常量贴在 li_det.py / 这里, 不走 env-var.
LIQ_REACH_THR_PX: int = 23         # locked-mode reach gate: l2s <= 23 视为已到刻度
LIQ_PRE_DET_L2S: int = 50          # LOCK_SCALE 阶段 wire l2s 占位值 (区分 first_frame 阶段的 10000)


def _g_l2s_above_reach(ctx: PhaseContext, event: Event) -> bool:
    """DOSING + LiquidUpdated 自循环守卫: l2s 还远 (>18px) 留在 DOSING.

    isinstance 检查是防御性: 理论上 LiquidUpdated 行只匹配 LiquidUpdated 事件, 但
    类型未匹配时直接返回 False, 让 SM 继续扫表.
    """
    if not isinstance(event, LiquidUpdated):
        return False
    return event.l2s > LIQ_REACH_THR_PX


# 注: 历史上还有 NotYetExposed / ExposureDone 两个 guard, 用于 SEARCH_TUBE+TubeDetected 分流
# (没曝光 → stay+跑曝光; 已曝光 → 转 LOCK_SCALE). 但 auto_expo + 找刻度需要在同帧连跑, 这两个
# guard 拆相会引入 1 帧延迟. 改回单一 row + Pipeline 同帧 extra handle LOCK_SCALE 后, 这两个 guard
# 就废弃了. action 内自己 idempotent (ctx.exposure_done 已 True 就 noop).
GUARDS: Dict[str, GuardFn] = {
    "L2sAboveReachThr":   _g_l2s_above_reach,
}


# ===========================================================================
#                          TRANSITIONS 表
# ===========================================================================
# 行顺序敏感. 前一行先匹配 (linear scan, O(n) per dispatch — n=16 行, 永远 < 1ms).
#
# 表读法 (五段):
#   1) IDLE 段:        CmdStart → SEARCH_TUBE
#   2) SEARCH_TUBE 段:  TubeDetected (NotYetExposed/ExposureDone 互斥分流) + TubeLost (self-loop)
#   3) LOCK_SCALE 段:   ScaleScored → DOSING (无守卫; handler 内部已包条件); TubeLost → SEARCH_TUBE
#   4) DOSING 段:       LiquidUpdated 自循环 (远) / CompletionConfirmed → COMPLETED /
#                      RecheckFailed → RECHECK_FAIL / TubeLost → SEARCH_TUBE
#   5) 通配批量段:      Cmd 事件在多个 phase 上行为一致, 用 list comprehension 批量生成.
# ===========================================================================

TRANSITIONS: List[Row] = [
    # ============ IDLE ============
    Row(Phase.IDLE,         CmdStart,            Phase.SEARCH_TUBE,  "StartSession"),

    # ============ SEARCH_TUBE ============
    # 单一 row: TubeDetected → 切 LOCK_SCALE + 跑 RunAutoExposureOnce (action 内部 idempotent:
    # ctx.exposure_done 已 True 就直接 return). Pipeline.run() 在同帧 SEARCH_TUBE→LOCK_SCALE 切相后
    # 立即跑一次 LockScalePhase.handle, 复现"同帧 auto_expo + 找刻度"时序.
    Row(Phase.SEARCH_TUBE,  TubeDetected,        Phase.LOCK_SCALE,   "RunAutoExposureOnce"),
    # SearchTubePhase 不再 emit TubeLost (skip_tube/st.ok=False 时改成只设 ctx.last_tube_ok=False, 留在
    # SEARCH_TUBE; wire status 由 to_wire_status(last_tube_ok=False) 输出 STATUS_NO_TUBE). 因此 TubeLost
    # 行从此表删除.

    # ============ LOCK_SCALE ============
    # handler 内部把 SCORE_BASE 自适应 + gap_stab 稳定性都做完, 仅在通过时才 emit ScaleScored.
    # SM 这边看到事件就转 DOSING (无 score 守卫; 条件全部 handler 包办).
    Row(Phase.LOCK_SCALE,   ScaleScored,         Phase.DOSING,       "FreezeScaleAndCalibrate"),
    # LockScalePhase 不 emit TubeLost (skip_tube / st.ok=False / center_inside_tube=False 都只设
    # ctx.last_tube_ok=False, 不切相 — 不重置 score/lock 状态, 让下一帧恢复时无缝继续).
    # LOCK_SCALE 阶段一旦进去就只能走 ScaleScored→DOSING 或 Cmd*.

    # ============ DOSING ============
    # l2s > 18 self-loop (远); l2s ≤ 18 时 handler 同帧也 emit CompletionConfirmed, 走下一行.
    Row(Phase.DOSING,       LiquidUpdated,       Phase.DOSING,       None,                  "L2sAboveReachThr"),
    Row(Phase.DOSING,       CompletionConfirmed, Phase.COMPLETED,    "EmitCompletionPacket"),
    Row(Phase.DOSING,       RecheckFailed,       Phase.RECHECK_FAIL, "EmitRecheckFailPacket"),
    # DOSING 阶段没有 TubeLost transition (DosingPhase 不 emit). 完成只通过 CompletionConfirmed 走
    # COMPLETED, 不主动切回 unlocked.

    # ============ 通配批量 ============
    # 这三组用 list comprehension 是为了避免重复列 6+ 行.
    # TERMINAL = (COMPLETED, RECHECK_FAIL):  收 CmdStart 重新跑一轮.
    *[Row(s, CmdStart, Phase.SEARCH_TUBE, "StartSession")      for s in TERMINAL],
    # ACTIVE = (SEARCH_TUBE, LOCK_SCALE, DOSING):  CmdPause 中途暂停, 回 IDLE.
    *[Row(s, CmdPause, Phase.IDLE,        "EndSessionPaused")  for s in ACTIVE],
    # CmdStop 在 ACTIVE + TERMINAL 5 个 phase 上一致行为: 回 IDLE.
    *[Row(s, CmdStop,  Phase.IDLE,        "EndSessionStopped") for s in ACTIVE + TERMINAL],
]
