# -*- coding: utf-8 -*-
# =============================================================================
# framework/upper_link.py —— 上位机协议适配器
# -----------------------------------------------------------------------------
# 一个 UpperLink 实例聚合 4 块功能:
#   self.run_state       : 运行/待机状态 (线程安全 int 容器, control_channel.RunState)
#   self._reader         : 15B 帧 RX + 回调 (control_channel.ControlReader, daemon 线程)
#   self.send()          : 7/8B status 包 TX + 自动 TCP 重连 (走 rt_util.send_detect_packet)
#   to_wire_status(...)  : phase + run_state + tube_ok + pause + dosing_first_frame → 协议字节
#                          STATUS_* (NO_TUBE=0/INIT=1/DETECTING=2/COMPLETED=3/PAUSED=4/STOPPED=5/RECHECK_FAIL=7)
#
# 线程模型 (UpperLink 是 cross-thread 胶水层):
#   - 主线程    : Pipeline.run() 每帧调 send (+ drain_cmds + sync heartbeat properties)
#   - daemon 1 : ControlReader._loop — select/recv 15B 帧, 解码后调 self._on_cmd
#                _on_cmd 仍在 daemon 线程上; 它做 3 件事:
#                  (a) 更新 self._last_cmd_* (heartbeat properties)   — 单字段赋值, 无锁
#                  (b) self.run_state.set(...)                         — 内部带锁
#                  (c) self.cmd_queue.put_nowait(Cmd* event)           — queue.Queue 自带锁
#                后两步是线程安全工具, (a) 依赖 CPython 单字段赋值原子保证.
#
# 队列与 properties 的分工:
#   - cmd_queue   : SM transition 触发源. CmdStart/CmdPause/CmdStop, 一次一个事件.
#                   Pipeline 主循环 drain_cmds 餵给 SM.feed_many.
#   - last_cmd_*  : "数据快照", 不触发 transition. 上位机 height/scale_height/volume 是
#                   heartbeat (CMD_NONE 心跳; 高频更新). Pipeline 每帧 sync 到 ctx.
#                   避免在 cmd_queue 里塞高频 heartbeat (会污染 SM debug log).
# =============================================================================
from __future__ import annotations

import queue
import socket
import threading
from typing import Optional

import rt_config
import rt_util
import control_channel
from control_channel import (
    CMD_NONE, CMD_PAUSE, CMD_START, CMD_STOP,
    STATE_IDLE, STATE_RUNNING,
    ControlReader, RunState,
)

from .event import CmdPause, CmdStart, CmdStop, Event
from .phase import Phase


# ===========================================================================
#                         to_wire_status 映射
# ===========================================================================
# 内部 Phase + 运行态 + 观察 → 协议字节 STATUS_*. 纯函数 (无副作用, 无默认值; 全部参数显式传).
#
# 映射逻辑 (优先级):
#   1) run_state == STATE_IDLE   → PAUSED (last_pause=True) 或 STOPPED
#   2) phase == IDLE             → INIT                                  (开机握手 / 未收 Start)
#   3) phase == SEARCH_TUBE      → DETECTING (tube_ok) 或 NO_TUBE
#   4) phase == LOCK_SCALE       → INIT (tube_ok) 或 NO_TUBE              (锁刻度前一直 INIT)
#   5) phase == DOSING           → INIT (dosing_first_frame) 或 DETECTING (鎖刻度當幀仍 INIT)
#   6) phase == COMPLETED        → COMPLETED
#   7) phase == RECHECK_FAIL     → RECHECK_FAIL
# ===========================================================================

def to_wire_status(
    phase: Phase,
    run_state: int,
    last_tube_ok: bool,
    last_pause: bool,
    dosing_first_frame: bool = False,
) -> int:
    """把内部 Phase + 运行态 + 试管观察 → 协议字节.

    `dosing_first_frame`: 鎖刻度那一幀 SM 已切 DOSING (FreezeScale action 跑完, ctx.dosing_first_frame=True),
    但 DosingPhase.handle 還沒跑 (要等下一幀). 這幀 wire 仍要報 INIT, 不能跳 DETECTING — 上位機根據
    "INIT → DETECTING" 邊沿觸發鎖刻度確認動作. dosing_first_frame=True 時返回 INIT, =False 時返回 DETECTING.
    """
    # 1) IDLE (上位机已发 Pause/Stop, 或开机还没收 Start) — 协议层覆盖 phase 信息
    if run_state == STATE_IDLE:
        return rt_util.STATUS_PAUSED if last_pause else rt_util.STATUS_STOPPED
    # 2-7) RUNNING 状态下按 phase 映射
    if phase == Phase.IDLE:
        return rt_util.STATUS_INIT
    if phase == Phase.SEARCH_TUBE:
        return rt_util.STATUS_DETECTING if last_tube_ok else rt_util.STATUS_NO_TUBE
    if phase == Phase.LOCK_SCALE:
        # 锁刻度之前一直 INIT; tube_ok=False (skip_tube / st.ok=False / inside=False) 返 NO_TUBE.
        return rt_util.STATUS_INIT if last_tube_ok else rt_util.STATUS_NO_TUBE
    if phase == Phase.DOSING:
        # frame N (鎖刻度當幀): dosing_first_frame=True → 仍報 INIT.
        # frame N+1 起 (DosingPhase first_frame 跑完設 False) → DETECTING.
        if dosing_first_frame:
            return rt_util.STATUS_INIT
        return rt_util.STATUS_DETECTING
    if phase == Phase.COMPLETED:
        return rt_util.STATUS_COMPLETED
    if phase == Phase.RECHECK_FAIL:
        return rt_util.STATUS_RECHECK_FAIL
    # 兜底 — 不应到达 (Phase 枚举只有 6 个值)
    return rt_util.STATUS_INIT


# ===========================================================================
#                              UpperLink
# ===========================================================================

class UpperLink:
    """上位机协议适配器 (TX 包 + RX 命令 + heartbeat 数据).

    用法 (typical, 由 main.py 装配):
        link = UpperLink(logger=logger)
        link.start()                              # 起 ControlReader 子线程, 建 TX socket
        ...
        cmd_events = link.drain_cmds()            # 主循环每帧拉队列里所有 Cmd 事件 → SM
        link.send(STATUS_DETECTING, gap_px, l2s)  # 主循环每帧发 status
        h = link.last_cmd_height                  # heartbeat 最新值; 主循环 sync 到 ctx
        link.run_state.is_running()               # send 内部用; 也可外部直查
        link.stop()

    线程模型详见模块级 docstring. 简言之: send / drain_cmds / properties 在主线程读;
    _on_cmd / _last_cmd_* / cmd_queue.put 在 Reader daemon 线程写; 都是线程安全的.
    """

    def __init__(self, logger: object = None, config=rt_config) -> None:
        self._logger = logger
        self._config = config

        # 运行态: ControlReader._on_cmd 写 (Reader 线程); send_detect_packet 读 (主线程).
        # control_channel.RunState 内部带 Lock, 跨线程读写安全.
        # 初始 IDLE — 等收到 CmdStart 才转 RUNNING.
        self.run_state = RunState(initial=STATE_IDLE)

        # ControlReader: daemon 线程跑 select/recv 解码 15B 帧, 解到帧调 handler=self._on_cmd.
        # update_socket(sock) 给它绑 socket; close() 让它退线程.
        self._reader = ControlReader(handler=self._on_cmd)

        # TX socket. send_detect_packet 内部会 reconnect 并返回新 sock; 所以这里保 mutable 引用,
        # 每次 send 都把可能更新的 sock 写回来.
        self._tx_sock: Optional[socket.socket] = None

        # send 可能从多源调用 (主循环每帧心跳 + EmitCompletionPacket action + 其它 force send).
        # 跨主线程调用 send 时 (理论上不会, 但保守加锁) 避免两个 sendall 交错.
        # 注: send_detect_packet 内部也会重连 _tx_sock, 这把锁也保护读写一致性.
        self._tx_lock = threading.Lock()

        # ============================================================
        # cmd_queue + heartbeat properties: 跨线程数据通道
        # ============================================================
        # cmd_queue: SM transition 触发源. Reader 线程 put, 主线程 drain.
        # maxsize=64: 上位机 cmd 频率 1-2 Hz, 64 帧足够吸收瞬时拥塞 (上位机连发 30 个命令也接得住).
        # 满了的 fallback 在 _on_cmd: drop oldest, push newest (保留最新动作).
        self.cmd_queue: "queue.Queue[Event]" = queue.Queue(maxsize=64)

        # heartbeat 数据快照: CMD_NONE 帧自带 height/scale_height/volume, 不触发 transition,
        # 但 ctx.last_cmd_height 是 SearchTubePhase 的 skip-tube gate 输入. 所以保 properties.
        # Reader 线程在 _on_cmd 里写, Pipeline 主线程读 (一次/帧 sync 到 ctx).
        # CPython 普通赋值在单字段上是原子的 (GIL 保证), 不加锁是为了避免主循环每帧获锁开销.
        # 容忍偶尔读到旧值 (旧→新切换瞬间 ~ns 级窗口, 下一帧自动校正).
        self._last_cmd_height: Optional[float] = None
        self._last_cmd_scale_height: Optional[float] = None
        self._last_cmd_volume: Optional[float] = None

    # ----------------------------- 生命周期 -----------------------------

    def start(self) -> None:
        """建 TX socket + 启 ControlReader 子线程. 失败时 logger.error 但不抛.

        TX_ENABLED=0 (env) 时跳过, 适合无上位机的本地开发 (cam 单独抓帧 / 算法调试).
        TCP 连接失败时不抛 — 会 log warning, send() 第一次调用时再尝试重连 (rt_util.send_detect_packet
        内部走 reconnect 分支).
        """
        if not self._config.TX_ENABLED:
            self._log("info", "[upper] TX disabled by config (TX_ENABLED=0)")
            return
        try:
            # tcp_connect: 关旧 sock + 新建 + control_reader.update_socket (让 Reader 在新 sock 上 loop)
            self._tx_sock = rt_util.tcp_connect(
                self._config.TX_ADDR,
                self._reader,
                old_sock=self._tx_sock,
            )
            self._log("info", f"[upper] connected to {self._config.TX_ADDR}")
        except Exception as exc:
            # 网络不通也让 Pipeline 跑起来 (camera 不依赖 TX). send() 自己会 reconnect.
            self._log("warning",
                      f"[upper] tcp_connect failed: {exc}; reader not bound, will retry on first send()")

    def stop(self) -> None:
        """关 ControlReader 子线程 + 关 TX socket. 不抛 (调用方关停时不应被 IO 错误打断)."""
        try:
            self._reader.close()    # 内部置 stop_evt + shutdown(SHUT_RD) + join(timeout=0.5s)
        except Exception:
            pass
        if self._tx_sock is not None:
            try: self._tx_sock.close()
            except Exception: pass
            self._tx_sock = None

    # 上下文协议 (可选). main.py 当前用 try/finally, 但 with UpperLink() as link: 也能跑.
    def __enter__(self) -> "UpperLink":
        self.start()
        return self
    def __exit__(self, *_a) -> None:
        self.stop()

    # ----------------------------- TX -----------------------------

    def send(
        self,
        status: int,
        gap_px: Optional[int],
        l2s_px: Optional[int] = 0,
        *,
        force: bool = False,
    ) -> None:
        """发送一个 status 包 (7B std 或 8B cpp 布局, 配置在 rt_config.TX_LAYOUT).

        force 语义:
          - True  : 绕过 run_state.is_running() 闸门. 用于开机握手 / Pause/Stop 确认 / Completion 上报 —
                    哪怕系统不在 RUNNING 状态也要发出 (开机握手 / Pause/Stop 确认 / Completion 上报).
          - False : 走 send_detect_packet 内部闸门 — 不在 RUNNING 状态时丢包. 这是心跳模式 — 主循环
                    每帧调一次, 但 IDLE 时实际不出 wire bytes.

        实现委托给 rt_util.send_detect_packet (复用其 TCP 重连逻辑); _tx_sock 反写是因为
        send_detect_packet 在 reconnect 时返回新 sock. _tx_lock 保护 _tx_sock 读写一致 (虽然 send
        通常只在主线程调, 但 EmitCompletionPacket action 也可能在 SM dispatch 中 reentrant 调过).
        """
        # ---- WIRE LOG: 每次实际 send 时把 wire 上的 status/gap/l2s 落到 logger ----
        # 排查上位机协议字段时直接对比 logger 输出.
        # will_drop 检查: send_detect_packet 内部 (not force AND not is_running) 时丢包,
        # 这里也别打印, 避免 IDLE/PAUSED 期间每帧刷屏.
        try:
            will_drop = (not force) and (not self.run_state.is_running())
            if not will_drop:
                gap_u16 = 10000 if gap_px is None else (int(gap_px) & 0xFFFF)
                l2s_u16 = 10000 if l2s_px is None else (int(l2s_px) & 0xFFFF)
                gap_i16 = gap_u16 - 65536 if gap_u16 >= 32768 else gap_u16
                l2s_i16 = l2s_u16 - 65536 if l2s_u16 >= 32768 else l2s_u16
                status_name = rt_util.STATUS_NAMES.get(int(status), int(status))
                self._log("info",
                    f"[wire] status={status_name}({int(status)}) "
                    f"gap_in={gap_px} l2s_in={l2s_px} "
                    f"u16: gap={gap_u16} l2s={l2s_u16} | i16: gap={gap_i16} l2s={l2s_i16} "
                    f"force={force} layout={self._config.TX_LAYOUT}"
                )
        except Exception:
            pass
        with self._tx_lock:
            self._tx_sock = rt_util.send_detect_packet(
                status, gap_px, l2s_px,
                force=force,
                config=self._config,
                tx_sock=self._tx_sock,
                run_state=self.run_state,
                control_reader=self._reader,
            )

    # ----------------------------- RX -----------------------------

    def poll_cmd(self, timeout: float = 0.0) -> Optional[Event]:
        """单事件拉取. 主循环用 drain_cmds (批量); poll 接口留给单元测试或单步调试."""
        try:
            return self.cmd_queue.get(block=(timeout > 0.0), timeout=timeout if timeout > 0.0 else None)
        except queue.Empty:
            return None

    def drain_cmds(self) -> "list[Event]":
        """一次性把队列里所有 Cmd 事件拉走 (主循环每帧调一次).

        返回顺序 = 入队顺序 (queue.Queue 是 FIFO). SM.feed_many 会按顺序 dispatch.
        队列空时返回 []; 主循环视为"本帧没有上位机命令".
        """
        out: "list[Event]" = []
        while True:
            try:
                out.append(self.cmd_queue.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def last_cmd_height(self) -> Optional[float]:
        """heartbeat 最新 height (Reader 线程写, 主循环 sync 到 ctx).

        语义: 上位机机械臂的当前高度 (mm). SearchTubePhase 用它做 skip-tube gate
        (height < SKIP_TUBE_HEIGHT_MIN=10 → 试管不在视野, 跳过检测).
        None = 还没收到任何 cmd 帧 (开机第一秒可能).
        """
        return self._last_cmd_height

    @property
    def last_cmd_scale_height(self) -> Optional[float]:
        """heartbeat 最新 scale_height (上位機"預設容量瓶刻度線高度", mm).
        LockScalePhase 用作 SCORE_BASE 採樣觸發 — height >= scale_height 時開始累積樣本.
        """
        return self._last_cmd_scale_height

    @property
    def last_cmd_volume(self) -> Optional[float]:
        """heartbeat 最新 volume. 协议占位字段, 算法不消费."""
        return self._last_cmd_volume

    # ----------------------------- 内部回调 -----------------------------

    def _on_cmd(self, cmd: int, height: Optional[float],
                scale_height: Optional[float], volume: Optional[float]) -> None:
        """ControlReader 解码完一帧调这里. 仍在 Reader daemon 线程上.

        参数: 见 control_channel.ControlReader 的 handler signature.
            cmd: int   — CMD_NONE/START/PAUSE/STOP
            height/scale_height/volume: Optional[float]  — 协议帧 4-15 字节, big-endian float

        4 件事:
          1) heartbeat 数据 (height/scale_height/volume) 一律写到 self._last_cmd_*. 任何 cmd 类型
             都带这些字段.
          2) CMD_NONE: 心跳, 不入 SM 队列 (避免污染事件流). 步骤 1 已经更新数据快照, 够了.
          3) CMD_START/PAUSE/STOP: 翻译成 Cmd* event 入队. 同时立即 self.run_state.set 切运行态 —
             这避免事件入队后到主线程 drain 之间的窗口期 send() 闸门状态不一致.
          4) 未知 cmd: warning log 后丢弃. 协议演进时可能出现新 cmd 字节, 不应崩.
        """
        # 1) heartbeat 数据快照. CPython 单字段赋值原子, 不加锁.
        # ---- DIAG: 監控 height 跳變, 看上位機 CMD_NONE trajectory 行為 ----
        # 條件: cmd_height 變化超過 5mm 才 log, 避免 1Hz 心跳刷屏.
        if height is not None:
            new_h = float(height)
            old_h = self._last_cmd_height
            if old_h is None or abs(new_h - old_h) >= 5.0:
                self._log("info", f"[upper-trace] cmd={cmd} height: {old_h} -> {new_h:.2f}")
            self._last_cmd_height = new_h
        if scale_height is not None:  self._last_cmd_scale_height = float(scale_height)
        if volume is not None:        self._last_cmd_volume = float(volume)

        # 2) CMD_NONE: 心跳, 走完步骤 1 就返回, 不入队
        if cmd == CMD_NONE:
            return

        # 3) 翻译 + 入队
        ev: Optional[Event] = None
        if cmd == CMD_START:
            # CmdStart 自身也带 height/etc; StartSession action 会 ctx.last_cmd_height = event.height,
            # 与步骤 1 写的 self._last_cmd_height 一致 (上位机的 START 帧总带 height).
            ev = CmdStart(height=height, scale_height=scale_height, volume=volume)
            # 立即切运行态 — 否则事件还在 cmd_queue 里时, send() 调用会因 is_running()=False 闸门丢包.
            self.run_state.set(STATE_RUNNING)
        elif cmd == CMD_PAUSE:
            ev = CmdPause()
            self.run_state.set(STATE_IDLE)
        elif cmd == CMD_STOP:
            ev = CmdStop()
            self.run_state.set(STATE_IDLE)
        else:
            self._log("warning", f"[upper] unknown cmd={cmd}")
            return

        # 4) 入队. queue.Queue 内部带 Lock; put_nowait 在满时抛 queue.Full.
        try:
            self.cmd_queue.put_nowait(ev)
        except queue.Full:
            # 满 = 主循环堵了 (8s+ 没 drain, 因为 64 帧 / 1-2 Hz cmd ≈ 30+ 秒). 异常情况.
            # 策略: drop oldest (拉一个出去), push newest. 保留最新动作; 上位机更可能想立即生效新命令.
            try: self.cmd_queue.get_nowait()
            except queue.Empty: pass
            try: self.cmd_queue.put_nowait(ev)
            except queue.Full: pass
            self._log("warning", "[upper] cmd_queue full; dropped oldest")

    # ----------------------------- 工具 -----------------------------

    def _log(self, level: str, msg: str) -> None:
        """容错日志: logger=None 回退 print; logger 没对应 level 方法 → 静默丢."""
        if self._logger is None:
            print(msg)
            return
        fn = getattr(self._logger, level, None)
        if callable(fn):
            try: fn(msg)
            except Exception: pass
