# -*- coding: utf-8 -*-
# =============================================================================
# main.py —— auto_volume_control 入口
# -----------------------------------------------------------------------------
# Pipeline (主线程) + StateMachine + PhaseHandler.
#
# 启动:
#   python3 main.py
# 调试 MJPEG 流:
#   DEBUG_STREAM_ENABLED=1 python3 main.py     # 然后浏览 http://127.0.0.1:8080/
# 关停:
#   Ctrl-C (SIGINT) 或 kill -TERM (SIGTERM) → stop_event 置位 → Pipeline.run() 跳出 →
#   按 *依赖反向* 顺序 stop 各 adapter.
#
# 进程结构:
#   主线程         : main() → with Camera() → pipeline.run()
#   daemon × 1     : UpperLink.ControlReader._loop  (cmd RX from 上位机)
#   daemon × 1     : VizRecorder.finalize loop      (viz 流落盘 + ffmpeg 收尾压缩)
#   daemon × 1     : Asynclogger.run                (日志落盘)
#   daemon × N     : DebugStream encoder + HTTP server (启用时 N≥2)
# =============================================================================
from __future__ import annotations

import signal
import sys
import threading

# 注意 import 顺序:
#   rt_config 必须最先 import, 副作用是把海康 MvImport 路径加进 sys.path.
#   后续 (camera_io / Camera adapter) 才能 import 到 MvCameraControl_class.
import rt_config  # noqa: F401
import rt_util
import debug_stream
from logger_utils import Asynclogger

from framework import (
    Camera, Phase, PhaseContext, Pipeline, StateMachine, UpperLink, VizRecorder,
    ACTIONS, GUARDS, TRANSITIONS, make_handlers,
)


def main() -> int:
    """主入口. 返回 exit code (0=正常, 1=异常)."""

    # ============================================================
    # 1) 信号处理: SIGINT (Ctrl-C) / SIGTERM 触发关停
    # ============================================================
    # threading.Event 是线程安全的; signal handler 在主线程的中断上下文里跑, 但 set() 操作
    # 不会阻塞或调用 Python 字节码以外的东西, 所以这里调用是 safe 的.
    # 这个 stop_event 同一个对象会传给 Pipeline; pipeline.run() 每轮检查 .is_set() 决定是否跳出.
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        print(f"[new-main] received signal {signum}, stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)   # Ctrl-C
    signal.signal(signal.SIGTERM, _on_signal)  # kill / docker stop

    # ============================================================
    # 2) Adapters (依赖正向顺序 startup; 关停时反向)
    # ============================================================
    # logger 最先起 — 其它 adapter 在构造期可能打日志. 不起 logger 它们就 fallback 到 print.
    logger = Asynclogger(name="AVC-new", print_to_console=True)
    logger.start()
    logger.info("[new-main] booting")

    # DEBUG_STREAM_ENABLED 仅控制 HTTP 服务, 默认不开端口.
    # 本地录像独立通过下方 recorder 钩子启用 JPEG 编码.
    debug_stream.start()

    # UpperLink: TX socket + Reader daemon. start() 失败 (网不通) 不抛, 走 reconnect 兜底.
    upper_link = UpperLink(logger=logger)
    upper_link.start()

    # VizRecorder: viz 流落盘 daemon + ffmpeg 收尾压缩.
    # - base_dir='logs' (与 log_*.txt 同位; 子目录 run_<ts>/ 由 open_run 创建)
    # - 启动后挂 debug_stream 的 encoder 钩子, 收到 push 的 JPEG 落到 run 目录
    # - close_run (StartSession 之外的 4 个 action) 入队 finalize → libx264 → 删源 jpeg
    # - 自管理保留: retain_runs / max_age_s / max_total_mb (任一闸触发即清旧 run_*)
    viz_recorder = VizRecorder(
        base_dir="logs",
        retain_runs=20,                         # 数量闸: 保留最近 20 个 run
        max_age_s=7 * 24 * 60 * 60,             # 时间闸: 7 天 (与 logger 同步)
        max_total_mb=500,                       # 大小闸: 500MB 上限
        framerate=5,                            # ffmpeg 编码帧率 (≈ 实际 detector fps)
        logger=logger,
    )
    viz_recorder.start()
    # 挂钩即启用本地录像所需的编码, 无需 DEBUG_STREAM_ENABLED=1.
    # encoder 线程在首次 push() 时惰性创建, 不会因此启动 HTTP 服务.
    debug_stream.set_recorder_hook(viz_recorder.on_encoded)

    # ============================================================
    # 3) StateMachine + PhaseHandler dict
    # ============================================================
    sm = StateMachine(
        initial=Phase.IDLE,
        transitions=TRANSITIONS,                # 21 行 Row 表 (state_machine.py)
        actions=ACTIONS,                        # 9 个真实 Action (actions.py)
        guards=GUARDS,                          # 3 个守卫 (state_machine.py)
        logger=logger,
    )
    handlers = make_handlers()  # 返回 dict: 6 个 Phase 各对应一个 PhaseHandler 实例
    sm.set_handlers(handlers)   # 让 SM 切相时能调 handler.on_enter / on_exit

    # ============================================================
    # 4) PhaseContext: 共享数据 + service refs
    # ============================================================
    # li_state / tube_tracker / gap_stab / score_base_samples 不在这里初始化:
    # StartSession action 收到 CmdStart 时会创建. 系统启动后 IDLE 阶段这些字段是 None,
    # 因此 IDLE handler (handle 永远返回 []) 不会读它们.
    # cam / upper_link / viz_recorder / logger 由 Pipeline.__init__ 自动注入到 ctx.
    ctx = PhaseContext()

    # ============================================================
    # 5) 进入主循环
    # ============================================================
    # Camera 上下文管理器: __enter__ 打开 SDK, __exit__ 关. 这样异常路径也能保证关相机.
    # Pipeline 不接管 Camera 生命周期 — Camera 由 with 块拥有, Pipeline 只是借用 .grab().
    exit_code = 0
    try:
        with Camera() as camera:
            pipeline = Pipeline(
                camera=camera,
                upper_link=upper_link,
                viz_recorder=viz_recorder,
                sm=sm,
                handlers=handlers,
                ctx=ctx,
                logger=logger,
                stop_event=stop_event,           # 与 signal handler 共享, SIGINT 触发 .set
                diagnostic_timing=rt_util._env_flag("PIPELINE_TIMING", default=False),
            )
            try:
                # 阻塞调用. stop_event 置位后 run() 自然退出.
                pipeline.run()
            except KeyboardInterrupt:
                # 双保险: 通常 SIGINT handler 已经 set 了 stop_event, run() 应该已经退出.
                # 如果 signal handler 没注册成功 (e.g. 在线程里跑) 才走这里.
                logger.info("[new-main] KeyboardInterrupt")
    except Exception as exc:
        # Camera open 失败 / 其它致命异常 → log 后退出非零.
        logger.error(f"[new-main] fatal: {exc}")
        exit_code = 1
    finally:
        # ============================================================
        # 6) 反初始化 (依赖反向)
        # ============================================================
        # Camera 在 with-block 自己关 (即使异常也走 __exit__).
        # 剩下的 daemon 按反序关:
        #   viz_recorder   : 若 SIGTERM 打断 dosing, 自动 close_run + drain finalize 队列
        #                    (drain_timeout=15s 给 ffmpeg 完成尾包; 超时 terminate 留源 jpeg)
        #   upper_link     : 关 ControlReader daemon + close TX socket
        #   debug_stream   : 关 HTTP server + encoder 线程 (要在 viz_recorder 之后, 因为
        #                    viz_recorder 的 finalize 依赖 encoder 线程把 in-flight 写完)
        #   logger        : 最后关; 关之前的 stop 信息也能写出去
        # 每个都 try/except, 关停异常不影响后续 adapter 关停.
        try: viz_recorder.stop(drain_timeout=15.0)
        except Exception as e: print(f"[new-main] viz_recorder.stop raised: {e}")
        try: upper_link.stop()
        except Exception as e: print(f"[new-main] upper_link.stop raised: {e}")
        try: debug_stream.set_recorder_hook(None)
        except Exception: pass
        try: debug_stream.stop()
        except Exception as e: print(f"[new-main] debug_stream.stop raised: {e}")
        try: logger.stop()
        except Exception as e: print(f"[new-main] logger.stop raised: {e}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
