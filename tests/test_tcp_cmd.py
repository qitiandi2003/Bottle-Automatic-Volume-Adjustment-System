#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCP 控制指令测试脚本
——————————————————
作为 TCP 服务端监听，等待 GrabImage_Cv_restart 连接后，
通过交互菜单向其发送控制帧。

帧格式 (15 字节, big-endian float):
  [0xAA] [cmd:1B] [height:4B] [scale_height:4B] [volume:4B] [0x55]

用法:
  python test_tcp_cmd.py              # 默认监听 0.0.0.0:9001
  python test_tcp_cmd.py 9002         # 指定端口
"""

import socket
import struct
import sys
import threading
import time

CMD_NONE  = 0
CMD_START = 1
CMD_PAUSE = 2
CMD_STOP  = 3

CMD_NAMES = {CMD_NONE: "NONE", CMD_START: "START", CMD_PAUSE: "PAUSE", CMD_STOP: "STOP"}

STATUS_NAMES = {
    0: "NO_TUBE", 1: "INIT", 2: "DETECTING", 3: "COMPLETED",
    4: "PAUSED", 5: "STOPPED", 7: "RECHECK_FAIL",
}


def build_frame(cmd: int, height: float = 0.0, scale_height: float = 0.0, volume: float = 0.0) -> bytes:
    """构造 15 字节控制帧。"""
    return struct.pack('>BBfffB', 0xAA, cmd & 0xFF, height, scale_height, volume, 0x55)


class RecvStats:
    """接收统计，后台线程写入，主线程读取。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.count = 0
        self.last_status = None
        self.last_gap = None
        self.last_l2s = None
        self.last_time = None
        self.changed = False   # 状态是否有变化（供主线程判断是否提示）

    def update(self, status, gap, l2s):
        with self.lock:
            changed = (status != self.last_status or gap != self.last_gap or l2s != self.last_l2s)
            self.last_status = status
            self.last_gap = gap
            self.last_l2s = l2s
            self.last_time = time.time()
            self.count += 1
            if changed:
                self.changed = True

    def snapshot(self):
        with self.lock:
            self.changed = False
            return {
                "count": self.count,
                "status": self.last_status,
                "gap": self.last_gap,
                "l2s": self.last_l2s,
                "time": self.last_time,
            }

    def pop_changed(self):
        with self.lock:
            c = self.changed
            self.changed = False
            return c


def recv_loop(conn: socket.socket, stop_evt: threading.Event, stats: RecvStats, verbose_flag: list):
    """后台接收并解析检测结果包。"""
    buf = bytearray()
    while not stop_evt.is_set():
        try:
            data = conn.recv(256)
        except (OSError, socket.error):
            break
        if not data:
            break
        buf.extend(data)
        while True:
            try:
                idx = buf.index(0xAA)
            except ValueError:
                buf.clear()
                break
            if idx > 0:
                del buf[:idx]
            # cpp 布局: 8 字节
            if len(buf) >= 8 and buf[6] == 0x55:
                status = buf[1]
                gap = (buf[2] << 8) | buf[3]
                l2s = (buf[4] << 8) | buf[5]
                stats.update(status, gap, l2s)
                if verbose_flag[0]:
                    sname = STATUS_NAMES.get(status, str(status))
                    print(f"\r  << [{sname}] gap={gap} l2s={l2s} (#{stats.count})")
                    print("> ", end="", flush=True)
                del buf[:8]
                continue
            # std 布局: 7 字节
            if len(buf) >= 7 and buf[6] == 0x55:
                status = buf[1]
                gap = buf[2] | (buf[3] << 8)
                l2s = buf[4] | (buf[5] << 8)
                stats.update(status, gap, l2s)
                if verbose_flag[0]:
                    sname = STATUS_NAMES.get(status, str(status))
                    print(f"\r  << [{sname}] gap={gap} l2s={l2s} (#{stats.count})")
                    print("> ", end="", flush=True)
                del buf[:7]
                continue
            # 不够一帧，等更多数据
            if len(buf) < 8:
                break
            buf.pop(0)
    if not stop_evt.is_set():
        print("\n[!] 连接断开")


def print_menu():
    print()
    print("========= 控制指令菜单 =========")
    print("  1       CMD_START (开始检测)")
    print("  2       CMD_PAUSE (暂停)")
    print("  3       CMD_STOP  (停止)")
    print("  h <N>   CMD_NONE + height  (如: h 50)")
    print("  f       自定义帧 (交互输入)")
    print("  s       查看最新接收状态")
    print("  v       开关接收详情打印")
    print("  q       退出")
    print("================================")


def interactive(conn: socket.socket):
    stop_evt = threading.Event()
    stats = RecvStats()
    verbose_flag = [False]  # 默认静默，不刷屏

    rx = threading.Thread(target=recv_loop, args=(conn, stop_evt, stats, verbose_flag), daemon=True)
    rx.start()

    print_menu()
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        parts = raw.lower().split()
        cmd_str = parts[0]

        if cmd_str == "q":
            break
        elif cmd_str in ("1", "start"):
            pkt = build_frame(CMD_START)
            conn.sendall(pkt)
            print(f">> CMD_START 已发送")
        elif cmd_str in ("2", "pause"):
            pkt = build_frame(CMD_PAUSE)
            conn.sendall(pkt)
            print(f">> CMD_PAUSE 已发送")
        elif cmd_str in ("3", "stop"):
            pkt = build_frame(CMD_STOP)
            conn.sendall(pkt)
            print(f">> CMD_STOP 已发送")
        elif cmd_str == "h":
            try:
                h = float(parts[1]) if len(parts) > 1 else float(input("  height = "))
            except (ValueError, EOFError, IndexError):
                print("  无效输入 (用法: h 50)")
                continue
            pkt = build_frame(CMD_NONE, height=h)
            conn.sendall(pkt)
            print(f">> CMD_NONE height={h} 已发送")
        elif cmd_str == "f":
            try:
                c = int(input("  cmd (0=NONE,1=START,2=PAUSE,3=STOP): "))
                h = float(input("  height [0]: ") or "0")
                sh = float(input("  scale_height [0]: ") or "0")
                vol = float(input("  volume [0]: ") or "0")
            except (ValueError, EOFError):
                print("  无效输入")
                continue
            pkt = build_frame(c, h, sh, vol)
            conn.sendall(pkt)
            print(f">> cmd={CMD_NAMES.get(c, c)} h={h} sh={sh} vol={vol} 已发送")
        elif cmd_str == "s":
            snap = stats.snapshot()
            if snap["status"] is None:
                print("  尚未收到数据")
            else:
                sname = STATUS_NAMES.get(snap["status"], str(snap["status"]))
                elapsed = f"{time.time() - snap['time']:.1f}s ago" if snap["time"] else ""
                print(f"  状态={sname}  gap={snap['gap']}  l2s={snap['l2s']}  累计={snap['count']}包  {elapsed}")
        elif cmd_str == "v":
            verbose_flag[0] = not verbose_flag[0]
            print(f"  接收详情打印: {'开启' if verbose_flag[0] else '关闭'}")
        elif cmd_str == "?":
            print_menu()
        else:
            print("  未知命令，输入 ? 查看菜单")

    stop_evt.set()
    conn.close()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    host = "0.0.0.0"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    print(f"[server] 监听 {host}:{port}，等待连接...")

    try:
        conn, addr = srv.accept()
        print(f"[server] 已连接: {addr}")
        interactive(conn)
    except KeyboardInterrupt:
        print("\n[server] 中断退出")
    finally:
        srv.close()
        print("[server] 已关闭")


if __name__ == "__main__":
    main()
