#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向 GrabImage_Cv_restart.py 持续发送固定控制信息的节点。
作为 TCP 服务端监听 rt_config.TX_ADDR，等待 GrabImage 连接后，按 control_channel 协议持续发送固定控制帧。
"""

from __future__ import annotations

import struct
import socket
import threading
import time
import sys
import os

# 与 GrabImage 使用同一配置，保证地址一致
from bottle_volume_control import config as rt_config
from bottle_volume_control.control.channel import CMD_START, CMD_NONE, CMD_PAUSE, CMD_STOP

# 默认发送的固定值（可按需修改）
DEFAULT_HEIGHT = 100.0
DEFAULT_SCALE_HEIGHT = 0.0
DEFAULT_VOLUME = 0.0
SEND_INTERVAL = 0.5  # 发送间隔（秒）


def build_control_packet(
    cmd: int,
    height: float = 0.0,
    scale_height: float = 0.0,
    volume: float = 0.0,
) -> bytes:
    """按 control_channel 协议组 15 字节控制帧：0xAA + cmd + 3×float(be) + 0x55。"""
    buf = bytearray(15)
    buf[0] = 0xAA
    buf[1] = int(cmd) & 0xFF
    struct.pack_into(">f", buf, 2, float(height))
    struct.pack_into(">f", buf, 6, float(scale_height))
    struct.pack_into(">f", buf, 10, float(volume))
    buf[14] = 0x55
    return bytes(buf)


def _drain_recv(sock: socket.socket, stop: threading.Event) -> None:
    """后台线程：读取并丢弃 GrabImage 发来的检测包，避免缓冲区满。"""
    try:
        while not stop.is_set():
            try:
                data = sock.recv(4096)
                if not data:
                    break
            except (OSError, socket.error):
                break
    except Exception:
        pass


def run():
    host, port = rt_config.TX_ADDR
    pkt_start = build_control_packet(
        CMD_START,
        DEFAULT_HEIGHT,
        DEFAULT_SCALE_HEIGHT,
        DEFAULT_VOLUME,
    )
    pkt_none = build_control_packet(
        CMD_NONE,
        DEFAULT_HEIGHT,
        DEFAULT_SCALE_HEIGHT,
        DEFAULT_VOLUME,
    )
    print(f"[fake_start] 监听 {host}:{port}，首次发 CMD_START，之后发 CMD_NONE，间隔 {SEND_INTERVAL}s")
    print("[fake_start] 等待 GrabImage_Cv_restart 连接...")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    server.settimeout(1.0)

    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            print(f"[fake_start] 已连接: {addr}")
            stop = threading.Event()
            t = threading.Thread(target=_drain_recv, args=(conn, stop), daemon=True)
            t.start()
            try:
                first_sent = False
                while True:
                    pkt = pkt_start if not first_sent else pkt_none
                    conn.sendall(pkt)
                    if not first_sent:
                        first_sent = True
                    time.sleep(SEND_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                print(f"[fake_start] 连接断开: {e}")
            finally:
                stop.set()
                try:
                    conn.close()
                except Exception:
                    pass
            print("[fake_start] 等待下一次连接...")
    except KeyboardInterrupt:
        print("\n[fake_start] 退出")
    finally:
        server.close()


if __name__ == "__main__":
    run()
