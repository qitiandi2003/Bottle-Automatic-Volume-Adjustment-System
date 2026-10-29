# -*- coding: utf-8 -*-
# =============================================================================
# framework/pipeline.py —— Pipeline 主循环编排
# -----------------------------------------------------------------------------
# 拓扑 (4 daemon + 1 主线程):
#   主线程        Pipeline.run() — 抓帧 → drain Cmd queue → handler.handle → SM.feed → send status
#   daemon × 1    UpperLink.ControlReader._loop      — cmd RX, put 到 cmd_queue
#   daemon × 1    AsyncLogger.run                    — 日志落盘
#   daemon × 1    VizRecorder.finalize loop           — viz 收尾 (ffmpeg) + retention
#   daemon × 1+   DebugStream encoder/HTTP server    — MJPEG 推流 (启用时)
#
# 单帧流程 (run() 循环每一轮):
#   0) sync heartbeat 字段 (Reader 线程 → ctx; CMD_NONE 等价路径)
#   1) drain UpperLink.cmd_queue → SM.feed_many   (Cmd*: CmdStart/CmdPause/CmdStop)
#   2) Camera.grab() → bgr (None=continue, 不增 frame_index)
#   3) ctx.current_bgr_frame = frame; ctx.tree = prepare_frame(frame)   (POST-MERGE trunk)
#   4) handler = handlers[SM.current]; sensor_events = handler.handle(frame, ctx)
#   5) SM.feed_many(sensor_events) — SM 决定切相 / 触发 action / 发协议包
#   5b) 同帧 SEARCH_TUBE → LOCK_SCALE extra-handle (一次)
#   6) UpperLink.send(to_wire_status(...), ctx.gap, ctx.l2s) — 心跳
#   7) Camera.release_frame() — 还回 SDK buffer pool
#
# 关停: stop_event 置位 → run() 跳出 while → 调用方 (main) 按"反初始化顺序"
# stop adapters (VizRecorder → UpperLink → DebugStream → AsyncLogger).
#
# 校验: __init__ 时 assert 6 个 Phase 都有对应 handler, 且 handler.phase 字段 = 该 key.
# 这保证 SM dispatch 时 handlers[sm.current] 永远不会 KeyError.
# =============================================================================
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .algo import prepare_frame
from .phase import Phase, PhaseContext, PhaseHandler
from .state_machine import StateMachine
from .upper_link import to_wire_status


# ===========================================================================
#                          StubPhaseHandler
# ===========================================================================
# 占位 handler — handle() 永远返回 []. 意图: 在真实 6 个 PhaseHandler 子类还没写完时,
# Pipeline 也能跑通 (结构性测试). 实际 ship 配置 (main.py) 用 phases.make_handlers()
# 构造真实 handler dict, 不再用 stub.
#
# Stub 模式下系统行为: SM 在 IDLE 等 CmdStart; 收到 CmdStart 切 SEARCH_TUBE; SearchStub
# 不 emit TubeDetected, 永远停在 SEARCH_TUBE; 上位机看到 STATUS_NO_TUBE 心跳.
# ===========================================================================

class StubPhaseHandler(PhaseHandler):
    """空实现: handle() 返回 []. 用于结构性测试 / 未实装相位的占位."""
    def __init__(self, phase: Phase) -> None:
        self.phase = phase

    def handle(self, frame, ctx: PhaseContext) -> List:
        return []


def make_stub_handlers() -> Dict[Phase, PhaseHandler]:
    """6 个 Phase 各一个 StubPhaseHandler. 仅用于 framework-only 烟测."""
    return {p: StubPhaseHandler(p) for p in Phase}


# ===========================================================================
#                              Pipeline
# ===========================================================================

class Pipeline:
    """主循环编排器.

    线程模型: 单线程 — 在 caller 线程上跑 run() (main.py 直接 main thread 调用).
    SM / handlers / 算法函数都在主线程跑; cv2 调用安全, OpenCV 内部 SIMD 由 cv2 自己管.
    并发只来自 daemon 线程的"对外服务" (cmd queue write / 日志写盘 / 图像写盘 / HTTP),
    经过 queue 隔离, 主循环不需要锁.

    构造参数全部 keyword-only — 防止 positional 误传 (字段太多, 顺序记不住).

    依赖注入:
        所有 adapter / sm / handlers / ctx 由调用方 (main.py) 组装好传入.
        Pipeline 不知道 camera_io / control_channel 的细节, 也不直接 import 算法模块 —
        那些都在 PhaseHandler 子类 + actions.py 里. 这层只做"管线时钟" + 调度.
    """

    # 协议心跳节流配置.
    # None = 每帧都发 (默认; send_detect_packet 每帧调用);
    # 数字 (秒) = 同一 status 这个时间窗内只发一次, 防 1+ kHz 帧率系统刷上位机.
    # 14 fps 相机不需要节流, 留接口给未来高帧率扩展.
    STATUS_THROTTLE_SAME_S: Optional[float] = None

    def __init__(
        self,
        *,
        camera: Any,
        upper_link: Any,
        viz_recorder: Any,
        sm: StateMachine,
        handlers: Dict[Phase, PhaseHandler],
        ctx: PhaseContext,
        logger: Any = None,
        stop_event: Optional[threading.Event] = None,
        diagnostic_timing: bool = False,
    ) -> None:
        # ---- 校验 handler 完备性 + phase 一致性 ----
        # 每个 Phase enum 值必须有自己的 handler 实例, 且 handler.phase == 该 key.
        # 这两条保证 dispatch 时 handlers[sm.current] 永远命中, 且不会用错 phase 的 handler.
        for p in Phase:
            if p not in handlers:
                raise ValueError(f"Pipeline: missing handler for {p.name}")
            h = handlers[p]
            if getattr(h, "phase", None) != p:
                raise ValueError(
                    f"Pipeline: handlers[{p.name}].phase mismatch "
                    f"(got {getattr(h, 'phase', None)!r}, expected {p!r})"
                )

        # ---- 注入 ----
        self._camera = camera
        self._upper_link = upper_link
        self._viz_recorder = viz_recorder
        self._sm = sm
        self._handlers = handlers
        self._ctx = ctx
        self._logger = logger
        self._diagnostic_timing = diagnostic_timing
        # stop_event=None 时自建一个; main 一般传共享 event, 让 SIGINT/SIGTERM 触发它.
        self._stop_event = stop_event or threading.Event()

        # ---- 把 service refs 注入 ctx ----
        # 让 actions / handlers 直接 ctx.upper_link.send(...) 之类, 不用 closure 也不用 self.adapter.
        # 这是 PhaseContext "数据 + 服务引用混合" 的取舍: 简化调用 vs. 更纯的数据层.
        # 选了前者 — 整个项目就这一个 Pipeline 实例, 没有"多 Pipeline 共享 ctx" 的复杂性.
        ctx.upper_link = upper_link
        ctx.viz_recorder = viz_recorder
        ctx.logger = logger
        # cam: Camera adapter 内部句柄. _cam 字段在 Camera.__enter__ 后才非 None;
        # 这里 try-get, run() 启动时再 sync 一次 (因为 with Camera() as cam: 是 main 层).
        ctx.cam = getattr(camera, "_cam", None)

        # ---- 让 SM 切相时能调 handler.on_enter / on_exit ----
        self._sm.set_handlers(self._handlers)

        # 节流缓存 (STATUS_THROTTLE_SAME_S 启用时用).
        self._last_status: Optional[int] = None
        self._last_status_ts: float = 0.0

    # ----------------------------- 控制 -----------------------------

    def stop(self) -> None:
        """触发主循环退出. threading.Event.set 是线程安全的, 可被信号处理器调用."""
        self._stop_event.set()

    # ----------------------------- 主循环 -----------------------------

    def run(self) -> None:
        """阻塞调用. 在 caller 线程上跑主循环. stop_event 置位后正常退出.

        循环不变式: 每轮迭代结束时, ctx 的字段 (last_tube_ok / gap / l2s 等) 描述的是
        "本帧 handler 处理完之后" 的状态. 下一轮开始前, heartbeat 字段被 sync 覆盖,
        其它字段在 handler 里被更新. 没有跨帧的脏数据.
        """
        self._log("info", "[pipeline] run loop starting")

        # Camera 在 main 层进入 with-block 后, _cam 字段才填上. 在 run() 里再 sync 一次,
        # 是因为 Pipeline.__init__ 时 Camera 还没 __enter__ (with 块在 main main() 里).
        self._ctx.cam = getattr(self._camera, "_cam", None)

        # 开机握手: 强制发一次当前 status (force=True 绕过 run_state.is_running 闸门), 让上位机
        # 能立刻看到设备就绪.
        self._send_status(force=True)

        # boot → IDLE 切相 hook (StateMachine 构造时 self.current=IDLE 不会自动触发 on_enter).
        # 让 IdlePhase.on_enter 统一处理"进入 IDLE 的可见提示" — 不论是 boot 还是 Cmd*/完成后再切回.
        # 异常隔离: hook 报错不影响主循环.
        try:
            initial_handler = self._handlers.get(self._sm.current)
            if initial_handler is not None:
                initial_handler.on_enter(self._ctx)
        except Exception as exc:
            self._log("error", f"[pipeline] initial on_enter raised: {exc}")

        while not self._stop_event.is_set():
            # ============================================================
            # 步骤 0: heartbeat sync (Reader 线程 → ctx)
            # ============================================================
            # UpperLink._on_cmd 在 Reader 线程里把 height/scale_height/volume 写入
            # self._last_cmd_*; 主线程这里读 properties 拷到 ctx, 让 handler 看到最新值.
            #
            # 同步在 cmd drain *之前*: 这样 CmdStart 事件被 SM dispatch 时, ctx.last_cmd_height
            # 已经反映了最新 heartbeat (CmdStart event 自身也带 height, StartSession action
            # 会再覆盖一次, 等价).
            self._ctx.last_cmd_height = self._upper_link.last_cmd_height
            self._ctx.last_cmd_scale_height = self._upper_link.last_cmd_scale_height
            self._ctx.last_cmd_volume = self._upper_link.last_cmd_volume

            # ============================================================
            # 步骤 1: Cmd 事件 drain → SM
            # ============================================================
            # cmd_queue 由 Reader 线程 put (异步), 主线程每帧 drain. 一次 drain 可能拿 0~N 个事件
            # (如果上位机连发 START + STOP, 会按 put 顺序餵给 SM).
            cmd_events = self._upper_link.drain_cmds()
            if cmd_events:
                self._sm.feed_many(cmd_events, self._ctx)

            # ============================================================
            # 步骤 2: 抓帧
            # ============================================================
            # camera.grab() 阻塞 ≤ 1000ms (camera_io.GetImageBuffer 超时). 拿不到帧返回 None,
            # 直接 continue 走下一轮 — 不增 frame_index, 不调 handler.
            #
            # **重要**: grab() 不 Free 相机 buffer — 处理完这一帧后, 循环尾部 step 7 才调
            # self._camera.release_frame() 把 SDK buffer 还回 pool. 防止 buffer pool 在处理期间
            # 累积旧帧导致视觉延迟. 各 step (handler / SM.feed_many / _send_status) 都吃异常,
            # 不会跳过 step 7, 所以不需要外层 try-finally 保护.
            frame = self._camera.grab()
            if frame is None:
                continue
            self._ctx.frame_index += 1

            # ============================================================
            # 步骤 3: 共享 trunk 准备 (POST-MERGE [A][B][C][D] 共享 gray_full)
            # ============================================================
            # current_bgr_frame: 给 actions 用 (handler 直接收 frame 参数, 不依赖 ctx).
            self._ctx.current_bgr_frame = frame
            # tree: 一次 cvtColor 给后续 handler / action 共享 — 多分支独立 cvtColor 合并到 trunk
            # 单次, 主循环节省 ~10ms/帧. tree 字段都是 view 或一次性新数组, 下一帧整体替换.
            self._ctx.tree = prepare_frame(frame)
            # 每帧顶部重置 tracker 复用标志. SearchTubePhase / LockScalePhase 谁先跑 tracker.update
            # 就置 True; 同帧另一个 phase (extra-handle 路径) 看到 True 跳过自己的 update —
            # 保证"每帧一次 tracker.update", 不让 SlewTubeTracker._frame_count 同帧多 ++.
            self._ctx.tube_state_fresh = False

            # ============================================================
            # 步骤 4: 当前 Phase 的 handler 处理
            # ============================================================
            # handler.handle 是 PhaseHandler 子类实现; 返回 0~N 个 Sensor 事件.
            # 任何 handler 异常都被 catch + log, 视作 "本帧没产出事件". 业务层后果由日志兜底.
            # 不让 handler 异常杀掉主循环 — 这是嵌入式系统的容错惯例.
            prev_phase = self._sm.current  # 记下 dispatch 前的 phase, 用于 step 5b 判定是否同帧切相
            handler = self._handlers[prev_phase]
            t0 = time.perf_counter() if self._diagnostic_timing else None
            try:
                sensor_events = handler.handle(frame, self._ctx) or []
            except Exception as exc:
                self._log("error",
                          f"[pipeline] handler({prev_phase.name}) raised: {exc}")
                sensor_events = []
            if t0 is not None:
                self._log("info", f"[TIME] phase={prev_phase.name} "
                          f"handler={(time.perf_counter()-t0)*1000:.1f}ms")
            # ============================================================
            # 步骤 5: sensor events → SM
            # ============================================================
            # SM dispatch 可能切相 + 跑 action (e.g. FreezeScaleAndCalibrate). action 内部
            # 可以发协议包 (force=True), 比下面的 step 6 心跳早一步.
            if sensor_events:
                self._sm.feed_many(sensor_events, self._ctx)

            # ============================================================
            # 步骤 5b: 同帧 SEARCH_TUBE → LOCK_SCALE extra-handle
            # ============================================================
            # SearchTubePhase emit TubeDetected → SM 切 LOCK_SCALE + RunAutoExposureOnce action.
            # 默认下一帧才跑 LockScalePhase.handle. 这里加一次 extra handle 让本帧 (tube_ok 当帧)
            # 也跑一次 LockScalePhase, 复现"同帧 auto_expo + 找刻度"时序.
            #
            # 边界:
            #   - 仅 SEARCH_TUBE→LOCK_SCALE 切相时触发. 其它切相 (LOCK_SCALE→DOSING 等) 不 extra —
            #     DosingPhase first_frame 分支也是 N+1 才跑.
            #   - LockScalePhase.handle 看到 ctx.tube_state_fresh=True 跳过自己的 tracker.update.
            #   - LockScalePhase 理论上可 emit ScaleScored 让 SM 再切到 DOSING, 但 SCORE_BASE 必须 ready
            #     且 gap_stab 必须稳 (10s 窗口), 同帧首次进入 LOCK_SCALE 不可能满足. 不加 chain 保护.
            if prev_phase == Phase.SEARCH_TUBE and self._sm.current == Phase.LOCK_SCALE:
                extra_handler = self._handlers[Phase.LOCK_SCALE]
                try:
                    extra_events = extra_handler.handle(frame, self._ctx) or []
                except Exception as exc:
                    self._log("error",
                              f"[pipeline] extra LOCK_SCALE handler raised: {exc}")
                    extra_events = []
                if extra_events:
                    self._sm.feed_many(extra_events, self._ctx)

            # ============================================================
            # 步骤 6: 心跳/状态包
            # ============================================================
            # 每帧发当前 phase 对应的 status. action 已发过的"事件包" force=True 会覆盖闸门;
            # 这里 force=False, run_state=IDLE 时 send_detect_packet 内部闸门会丢包.
            self._send_status(force=False)

            # ============================================================
            # 步骤 7: 释放相机 buffer
            # ============================================================
            # 处理完一帧后才把 SDK buffer 还回 pool. 各 step 都吃异常, 不会跳过这里;
            # release_frame 自身 no-throw.
            self._camera.release_frame()

        self._log("info", "[pipeline] run loop stopped")

    # ----------------------------- 内部 -----------------------------

    def _send_status(self, *, force: bool) -> None:
        """计算 wire status + 走 UpperLink.send. 节流逻辑也在这里 (默认关闭)."""
        # to_wire_status 是纯函数 (upper_link.py 内). 输入 (phase, run_state, last_tube_ok,
        # last_pause), 输出 STATUS_* (0-5/7).
        status = to_wire_status(
            phase=self._sm.current,
            run_state=self._upper_link.run_state.get(),
            last_tube_ok=self._ctx.last_tube_ok,
            last_pause=self._ctx.last_cmd_pause,
            dosing_first_frame=self._ctx.dosing_first_frame,
        )
        # 节流分支: 同一 status 在 STATUS_THROTTLE_SAME_S 秒内不重发 (force 时无视).
        # 默认 None 关闭, 每帧都走 send (旧行为).
        if not force and self.STATUS_THROTTLE_SAME_S is not None:
            now = time.monotonic()
            if status == self._last_status and (now - self._last_status_ts) < self.STATUS_THROTTLE_SAME_S:
                return
            self._last_status_ts = now
        self._last_status = status
        # send 内部有 run_state.is_running() 闸门 + TCP 重连; force=True 跳过闸门.
        self._upper_link.send(status, self._ctx.gap, self._ctx.l2s, force=force)

    def _log(self, level: str, msg: str) -> None:
        """容错的日志接口. 没注入 logger 时回退 print (便于裸 Pipeline 调试)."""
        if self._logger is None:
            print(msg)
            return
        fn = getattr(self._logger, level, None)
        if callable(fn):
            try: fn(msg)
            except Exception: pass
