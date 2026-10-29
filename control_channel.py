#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple control channel helpers for start/pause commands over TCP.

================================================================================
模块作用：
    和上位机（PLC / 主控服务器）之间的"控制通道"。
    上位机通过 TCP 推送 15 字节的命令帧告诉设备："开始 / 暂停 / 停止 + 当前瓶高/刻度高/目标容量"。
    本模块负责：
        1) RunState —— 当前运行状态（IDLE/RUNNING）的线程安全持有者；
        2) ControlReader —— 在一条后台线程里非阻塞地 select/recv，解析出帧并回调 handler。

协议帧格式（总长 15 字节 = ext_len）：
    [0]    0xAA          帧头
    [1]    cmd           命令字（CMD_NONE/START/PAUSE/STOP）
    [2..5] height        float32 big-endian，瓶子高度
    [6..9] scale_height  float32 big-endian，刻度位置（相对瓶底）
    [10..13] volume      float32 big-endian，目标容量
    [14]   0x55          帧尾（若非 0x55 视为错帧，丢弃 0xAA 重找下一个）
================================================================================
"""

from __future__ import annotations       # 让类型注解行为懒求值（不影响运行）

import select                            # 非阻塞 IO 多路复用，避免 recv 独占线程
import socket                            # TCP 读数据
import struct                            # 解析 big-endian float32
import threading                         # 线程 + Lock + Event
from typing import Callable, Optional    # 仅为了标注类型提示

# ---------------- 命令字枚举 ----------------
CMD_NONE  = 0   # 空命令（心跳 / 无操作）
CMD_START = 1   # 开始注液
CMD_PAUSE = 2   # 暂停
CMD_STOP  = 3   # 停止

# ---------------- 运行态枚举 ----------------
STATE_IDLE    = 0    # 空闲（未启动或已停止）
STATE_RUNNING = 1    # 任务进行中


class RunState:
    """线程安全的 int 状态容器。多线程都会读/写它，所以值的改动都走 Lock。"""
    def __init__(self, initial: int = STATE_IDLE) -> None:
        # 用 int 保存实际状态值；Lock 保证 get/set 原子
        self._value = int(initial)
        self._lock = threading.Lock()

    def get(self) -> int:
        """读取当前状态（加锁读）。"""
        with self._lock:
            return self._value

    def set(self, new_value: int) -> bool:
        """
        写入新状态。
        返回 True 表示实际发生了变化；False 表示和原值相同未改动。
        这个返回值设计可以让调用方只在"真变化"时打印日志 / 触发副作用。
        """
        with self._lock:
            if self._value == new_value:
                return False
            self._value = int(new_value)
            return True

    def is_running(self) -> bool:
        """是否处于运行态的快捷问法。"""
        return self.get() == STATE_RUNNING


class ControlReader:
    """
    控制帧接收器：在后台线程里非阻塞地从一个 TCP socket 上接收控制帧。

    特性：
      - 运行时可用 update_socket() 替换底层 socket（发生 TCP 重连时调用）；
      - set_handler() 让调用方自由替换业务回调；
      - close() 统一的优雅关闭：设置 stop event + shutdown(SHUT_RD) 唤醒阻塞的 select。
    """
    def __init__(
        self,
        handler: Optional[Callable[[int, Optional[float], Optional[float], Optional[float]], None]] = None,
    ) -> None:
        """
        参数 handler：接收帧后回调；签名为
            handler(cmd: int, height: Optional[float], scale_height: Optional[float], volume: Optional[float])
        传 None 表示先占位，后续再 set_handler()。
        """
        self._handler = handler
        self._lock = threading.Lock()                    # 守护 thread/stop/sock 三者的一致性
        self._thread: Optional[threading.Thread] = None  # 后台接收线程
        self._stop: Optional[threading.Event] = None     # 线程退出信号
        self._sock: Optional[socket.socket] = None       # 当前绑定的 socket

    def set_handler(
        self,
        handler: Optional[Callable[[int, Optional[float], Optional[float], Optional[float]], None]],
    ) -> None:
        """替换回调。运行中替换也是安全的，因为加了锁。"""
        with self._lock:
            self._handler = handler

    def update_socket(self, sock: Optional[socket.socket]) -> None:
        """
        绑定/替换当前读取的 socket：
            - 若与旧 sock 为同一对象，不处理；
            - 否则先关旧线程/旧 sock，再用新 sock 起一条新后台线程；
            - sock=None 表示只做"解绑"。
        """
        with self._lock:
            if sock is self._sock:           # 相同对象，直接返回，避免无意义重启
                return
            self._close_locked()             # 安全拆掉旧的读取循环
            self._sock = sock
            if sock is None:                 # 调用方只想取消读取，那就不再起新线程
                return
            # 为新线程单独创建一个 Event；每次 update_socket 都是一组全新的 thread+stop
            stop_evt = threading.Event()
            self._stop = stop_evt
            th = threading.Thread(target=self._loop, args=(sock, stop_evt), daemon=True)
            self._thread = th
            th.start()
            print("[ctrl] reader attached to socket")

    def _close_locked(self) -> None:
        """
        内部关闭函数，调用前必须已持有 self._lock。
        做三件事：
            1) 置位 stop_evt 通知 _loop 退出；
            2) shutdown(SHUT_RD) 打断正在等待的 select/recv；
            3) join 线程（超时 0.5s，防止业务卡死）。
        """
        if self._thread and self._thread.is_alive():
            if self._stop:
                self._stop.set()             # 告诉 _loop 循环条件变 False
            try:
                if self._sock:
                    # shutdown(SHUT_RD) 会让 recv 立刻返回空/抛 OSError，从而解除 select 阻塞
                    self._sock.shutdown(socket.SHUT_RD)
            except Exception:
                pass                         # socket 已坏或未连，忽略
            self._thread.join(timeout=0.5)   # 最多等半秒；超时就强制丢弃引用
        self._thread = None
        self._stop = None
        self._sock = None

    def close(self) -> None:
        """外部优雅关闭入口：内部全程持锁，然后委托给 _close_locked。"""
        with self._lock:
            self._close_locked()

    def _loop(self, sock: socket.socket, stop_evt: threading.Event) -> None:
        """
        后台线程主循环：持续从 sock 读字节，解析控制帧。
        使用 select.select(..., 0.5) 做带超时的可读等待，每 0.5s 检查一次 stop 信号。

        缓冲区 buf 是一个 bytearray，每次把收到的字节追加进去，再做"找 0xAA → 取 15 字节 → 校验 0x55"的拆包。
        """
        buf = bytearray()
        ext_len = 15                          # 协议固定帧长
        while not stop_evt.is_set():
            try:
                # 只读事件等 0.5 秒；超时返回空列表，用于周期性检查 stop_evt
                ready, _, _ = select.select([sock], [], [], 0.5)
            except Exception:
                break                         # select 出错（fd 被关等）→ 直接结束线程
            if not ready:
                continue                      # 0.5 s 内无数据，回头检查 stop
            try:
                data = sock.recv(64)          # 一次最多读 64 字节；和帧长无强关联
            except (OSError, socket.error):
                break                         # 读错 → 连接出问题 → 退出线程
            if not data:
                break                         # recv 返回空 bytes 表示对端关闭
            buf.extend(data)                  # 追加到累积缓冲，等拆帧
            # 内层 while：把当前 buf 里能拆出来的完整帧都消费掉
            while True:
                try:
                    idx = buf.index(0xAA)     # 找到下一个帧头位置
                except ValueError:
                    buf.clear()               # 没有帧头 → 缓冲里没有可用数据，全清掉
                    break
                remaining = len(buf) - idx    # 自帧头起还剩多少字节
                if remaining < ext_len:
                    # 不够一个完整帧：丢掉帧头之前的垃圾，保留从帧头开始的片段等下次
                    buf = buf[idx:]
                    break
                # ========= 这里开始解析一个候选帧 =========
                parsed = False                # 是否成功解析出一帧
                cmd = None
                height = None
                scale_height = None
                volume = None
                # 校验帧尾必须是 0x55；偏移 idx+14 即帧的第 15 个字节
                if buf[idx + 14] == 0x55:
                    cmd = buf[idx + 1]        # 命令字节，对应 CMD_NONE/START/PAUSE/STOP
                    # 接着三段 float32（大端）：height / scale_height / volume
                    # >f 的 '>' 表示 big-endian，'f' 表示 4 字节浮点
                    try:
                        height = struct.unpack('>f', bytes(buf[idx + 2:idx + 6]))[0]  # big-endian float
                    except Exception:
                        height = None         # 解析失败就给 None，让业务方自己判空
                    try:
                        scale_height = struct.unpack('>f', bytes(buf[idx + 6:idx + 10]))[0]
                    except Exception:
                        scale_height = None
                    try:
                        volume = struct.unpack('>f', bytes(buf[idx + 10:idx + 14]))[0]
                    except Exception:
                        volume = None
                    # 消费掉这 15 字节（包括帧头前的无用字节）
                    del buf[:idx + ext_len]
                    parsed = True
                else:
                    # 帧尾不匹配：疑似错位帧头 → 只丢掉这 1 个 0xAA，让下一轮从更后面重新找
                    buf.pop(idx)
                if not parsed:
                    # 没解析出来（上面 else 分支），内循环继续尝试下一个 0xAA
                    continue
                # ========= 解析成功后的副作用：打日志 + 调回调 =========
                if cmd != CMD_NONE:
                    # CMD_NONE 基本是心跳，太频繁不打；其他命令都落一条日志便于排查
                    if scale_height is None and volume is None:
                        print(f"[ctrl] received cmd={cmd} height={height}")
                    else:
                        print(f"[ctrl] received cmd={cmd} height={height} scale_h={scale_height} volume={volume}")
                # 抓 handler 引用再调用（set_handler 可能并发替换，先取本地引用更稳）
                handler = self._handler
                if handler is not None:
                    try:
                        handler(cmd, height, scale_height, volume)
                    except Exception as exc:
                        # 业务回调不应让读取线程挂掉，一律捕获+打印
                        print(f"[ctrl] handler error: {exc}")
        # while 结束：外部 stop 或 socket 出错 → 确保 stop_evt 已置位
        stop_evt.set()
