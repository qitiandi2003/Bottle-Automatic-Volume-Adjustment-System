# -*- coding: utf-8 -*-
# =============================================================================
# debug_stream.py —— MJPEG 調試流模塊 (stdlib-only)
# -----------------------------------------------------------------------------
# 職責:
#   1) 提供給 detector / li_det / tube / center_scale 等模塊一個極輕的 push(name, frame) 接口
#   2) 後台 HTTP 服務把最新幀以 multipart/x-mixed-replace (MJPEG) 推給瀏覽器
#   3) HTTP 與本地錄像獨立; 兩者都未啟用時 push() 幾乎零開銷
#
# 設計:
#   - push(name, frame): detector 線程調用, O(1), 非阻塞. 只把幀塞進該 stream 對應的
#     size=1 隊列 (隊列滿則丟舊幀), 不做編碼.
#   - 每個 stream name 啟一個 encoder 線程, 從隊列 get → 可選降採樣 → cv2.imencode JPEG
#     → 把結果存到 _Slot.latest_jpeg, 用 Condition 通知所有 HTTP handler.
#   - HTTP handler 在 /stream/<name> 上阻塞等 Condition, 有新幀就用 multipart 寫出去.
#     一個 stream 可以被多個瀏覽器同時訂閱, encoder 只跑一遍.
#   - 首次 push 某個 name 時才惰性創建 _Slot, 所以 detector 代碼不用提前註冊 stream 名.
#
# 對外 API (模塊級函數, 保持極簡):
#   debug_stream.start()           # 讀環境變量, 啟 HTTP server. 非 enabled 時直接返回.
#   debug_stream.push(name, frame) # 任何時候都安全. 不 enabled 或 name 還沒 encoder 則純開銷~0.
#   debug_stream.stop()            # 停 server + 所有 encoder
#   debug_stream.is_enabled()      # HTTP 或本地錄像需要編碼時為 True
#
# 環境變量:
#   DEBUG_STREAM_ENABLED  1/0, 默認 0
#   DEBUG_STREAM_HOST     綁定地址, 默認 0.0.0.0 (LAN 內都能訪問)
#   DEBUG_STREAM_PORT     默認 8080
#   DEBUG_STREAM_MAX_WIDTH 寬度上限, 默認 640. 源 3072×2048 會被縮到 640×427 再編碼 (3:2 比例)
#   DEBUG_STREAM_QUALITY  JPEG quality 0-100, 默認 75
# =============================================================================
from __future__ import annotations

import html
import os
import queue
import socket
import socketserver
import sys
import tarfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional

import cv2
import numpy as np
from ..paths import logs_dir


# 视为"viz 活跃"的判据: 任一 stream 在最近 VIZ_IDLE_THRESHOLD_S 秒内推过帧.
# 长跑下用于 /logs/download 阀门 — viz 活跃时拒绝下载,避免压缩与 MJPEG 编码竞争 CPU.
VIZ_IDLE_THRESHOLD_S = 5.0


# ---------------- env 配置 (讀一次, 寫死到模塊級變量) ----------------

def _env_flag(name: str, default: bool = False) -> bool:
    """兼容 0/1/true/false/yes/no/on/off 的 bool 環境變量讀取."""
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ('0', 'false', 'no', 'off', '')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


_ENABLED = _env_flag('DEBUG_STREAM_ENABLED', default=False)
_HOST = os.getenv('DEBUG_STREAM_HOST', '0.0.0.0')
_PORT = _env_int('DEBUG_STREAM_PORT', 8080)
_MAX_WIDTH = _env_int('DEBUG_STREAM_MAX_WIDTH', 640)
_QUALITY = max(1, min(100, _env_int('DEBUG_STREAM_QUALITY', 75)))


# ---------------- 內部狀態 ----------------

class _Slot:
    """單個 stream name 的狀態容器.

    - raw_queue: size=1 的原始幀隊列. detector 線程 put_nowait, encoder 線程 get.
    - latest_jpeg / latest_ts: encoder 寫入, HTTP handler 讀取, 通過 cond 同步.
    - cond: Condition, encoder notify_all, handler wait_for.
    - encoder_thread: 專屬 JPEG 編碼線程.
    - shutdown: 停止信號, encoder 線程看到就退出.
    """
    __slots__ = (
        'name', 'raw_queue', 'cond', 'latest_jpeg', 'latest_ts',
        'encoder_thread', 'shutdown', 'frames_in', 'frames_out'
    )

    def __init__(self, name: str):
        self.name = name
        self.raw_queue: queue.Queue = queue.Queue(maxsize=1)
        self.cond = threading.Condition()
        self.latest_jpeg: Optional[bytes] = None
        self.latest_ts: float = 0.0            # 單調遞增, HTTP handler 用 > last_ts 判新幀
        self.encoder_thread: Optional[threading.Thread] = None
        self.shutdown = threading.Event()
        self.frames_in = 0                     # 統計: push 進來的幀數
        self.frames_out = 0                    # 統計: 編碼成功的幀數


# 全局 slot 字典 + 鎖. 首次 push 某 name 時在鎖內創建 slot.
_slots: Dict[str, _Slot] = {}
_slots_lock = threading.Lock()

# HTTP server 和它的後台線程
_server: Optional[ThreadingHTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_server_started = threading.Event()


# ---------------- encoder 線程 ----------------

def _encoder_loop(slot: _Slot) -> None:
    """把 raw_queue 裏的 numpy 幀編成 JPEG, 放到 slot.latest_jpeg."""
    max_w = _MAX_WIDTH
    quality = _QUALITY
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    while not slot.shutdown.is_set():
        try:
            # timeout 是為了能定期檢查 shutdown; 無新幀就繼續等
            frame = slot.raw_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if frame is None:               # 哨兵值: stop() 推入用來喚醒 get
            break
        try:
            # 下採樣 (只縮小, 不放大)
            if max_w > 0 and frame.shape[1] > max_w:
                ratio = max_w / float(frame.shape[1])
                new_h = max(1, int(round(frame.shape[0] * ratio)))
                frame = cv2.resize(frame, (max_w, new_h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode('.jpg', frame, encode_params)
            if not ok:
                continue
            data = buf.tobytes()
        except Exception:
            continue                    # 單幀編碼失敗不影響後續
        with slot.cond:
            slot.latest_jpeg = data
            slot.latest_ts = time.time()
            slot.frames_out += 1
            slot.cond.notify_all()
        # tee 给 recorder (viz_recorder.on_encoded). 未挂钩零成本.
        # 在 cond 块外调 — 钩子做磁盘 IO 不该握 slot 的 cond.
        hook = _recorder_hook
        if hook is not None:
            try:
                hook(slot.name, data)
            except Exception:
                pass


def _get_or_create_slot(name: str) -> _Slot:
    """返回 name 對應的 slot, 不存在則創建並啟 encoder 線程."""
    with _slots_lock:
        slot = _slots.get(name)
        if slot is None:
            slot = _Slot(name)
            th = threading.Thread(
                target=_encoder_loop, args=(slot,),
                name=f'debug_stream.enc.{name}', daemon=True,
            )
            slot.encoder_thread = th
            th.start()
            _slots[name] = slot
        return slot


# ---------------- recorder 钩子 ----------------
# tee 出 encoder 线程已经编出的 JPEG 字节给外部 (viz_recorder 落盘 + 收尾 H.264 压缩).
# 设计要点:
#   - main 启动期调 set_recorder_hook(viz_recorder.on_encoded) 挂钩, stop 期清 None;
#   - encoder loop 在 slot.latest_jpeg 赋值并 notify_all 后立刻调钩子;
#   - 钩子签名 (stream_name, jpeg_bytes) -> None; 必须非阻塞 (~5-20ms) 且永不抛.
#   - 不挂钩时 (默认) 一次属性读零成本; 不影响纯 MJPEG 推流场景.
_recorder_hook: Optional[Callable[[str, bytes], None]] = None


def set_recorder_hook(fn: Optional[Callable[[str, bytes], None]]) -> None:
    """注册本地录像钩子并允许 JPEG 编码; fn=None 卸钩.

    本函数不会启动 HTTP 服务. HTTP 仍只由 DEBUG_STREAM_ENABLED 控制.

    并发模型: 全局变量, GIL 保证读写原子; encoder 线程读一次快照后调用, 在调用期间外部
    set_recorder_hook(None) 不会让 encoder 拿到 None ref.
    """
    global _recorder_hook
    _recorder_hook = fn


# ---------------- 日志下载支撑 ----------------
# 把 logs/ 目录定位与 viz 活跃判定都收在模块级,handler 直接复用. 不引入新依赖.

def _get_logs_dir() -> str:
    """Use the same generated-data directory as the logger and recorder."""
    return str(logs_dir())


def _is_viz_busy() -> bool:
    """任一 stream 在最近 VIZ_IDLE_THRESHOLD_S 秒内推过帧 → 视为活跃.

    用途: /logs/download 阀门. 活跃时拒绝下载, 避免 tar.gz 压缩 CPU 与 MJPEG 编码竞争.
    仅本地录像开启时也会更新 latest_ts, 避免下载与录像争抢 CPU.
    """
    now = time.time()
    with _slots_lock:
        for slot in _slots.values():
            if slot.latest_ts > now - VIZ_IDLE_THRESHOLD_S:
                return True
    return False


# ---------------- HTTP handler ----------------

_BOUNDARY = 'frame'
_INDEX_CSS = (
    'body{font-family:sans-serif;background:#111;color:#ddd;margin:16px}'
    'h1{font-size:18px}img{max-width:640px;border:1px solid #333;margin:4px}'
    'a{color:#4af;text-decoration:none}a:hover{text-decoration:underline}'
    '.row{margin-bottom:16px}.meta{color:#888;font-size:12px}'
)


class _StreamHandler(BaseHTTPRequestHandler):
    server_version = 'DebugStream/1.0'

    # 抑制默認訪問日誌 (會污染 detector stdout)
    def log_message(self, format, *args):
        return

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/' or path == '/index.html':
            self._serve_index()
        elif path.startswith('/stream/'):
            self._serve_stream(path[len('/stream/'):])
        elif path.startswith('/snapshot/'):
            self._serve_snapshot(path[len('/snapshot/'):])
        elif path == '/logs':
            self._serve_logs_index()
        elif path == '/logs/download':
            self._serve_logs_download()
        elif path == '/health':
            self._serve_simple(200, 'text/plain', b'ok\n')
        else:
            self._serve_simple(404, 'text/plain', b'not found\n')

    # ---- 小工具 ----
    def _serve_simple(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- / : 索引頁 ----
    def _serve_index(self):
        with _slots_lock:
            names = sorted(_slots.keys())
            rows = []
            for n in names:
                slot = _slots[n]
                age = (time.time() - slot.latest_ts) if slot.latest_ts > 0 else -1
                meta = f'in={slot.frames_in} out={slot.frames_out} age={age:.1f}s' if age >= 0 else 'no frame yet'
                safe = html.escape(n)
                rows.append(
                    f'<div class="row"><div><a href="/stream/{safe}">{safe}</a> '
                    f'<span class="meta">({html.escape(meta)})</span></div>'
                    f'<img src="/stream/{safe}" alt="{safe}"></div>'
                )
        body_html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>debug_stream</title><style>{_INDEX_CSS}</style></head><body>'
            f'<h1>auto_volume_control debug streams ({len(names)} active)</h1>'
            + ('<p>no streams yet. start detector with DEBUG_STREAM_ENABLED=1 and wait for first push.</p>' if not names else '')
            + ''.join(rows)
            + '<p><a href="/logs">→ view / download logs</a></p>'
            + '</body></html>'
        )
        self._serve_simple(200, 'text/html; charset=utf-8', body_html.encode('utf-8'))

    # ---- /snapshot/<name> : 單張 JPEG ----
    def _serve_snapshot(self, name: str):
        with _slots_lock:
            slot = _slots.get(name)
        if slot is None or slot.latest_jpeg is None:
            self._serve_simple(404, 'text/plain', b'no frame\n')
            return
        with slot.cond:
            data = slot.latest_jpeg
        self._serve_simple(200, 'image/jpeg', data)

    # ---- /stream/<name> : MJPEG 流 ----
    def _serve_stream(self, name: str):
        with _slots_lock:
            slot = _slots.get(name)
        if slot is None:
            self._serve_simple(404, 'text/plain', b'stream not active yet\n')
            return

        # multipart header
        try:
            self.send_response(200)
            self.send_header('Age', '0')
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={_BOUNDARY}')
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        last_ts = 0.0
        try:
            while not slot.shutdown.is_set():
                with slot.cond:
                    # 等到出現比 last_ts 更新的幀, 最長 5s. 超時則重新判 shutdown.
                    got = slot.cond.wait_for(
                        lambda: slot.latest_ts > last_ts or slot.shutdown.is_set(),
                        timeout=5.0,
                    )
                    if slot.shutdown.is_set():
                        break
                    if not got or slot.latest_jpeg is None:
                        continue
                    data = slot.latest_jpeg
                    ts = slot.latest_ts
                last_ts = ts
                # 寫 multipart part: 邊界 + Content-Type + Content-Length + 空行 + 數據 + 空行
                head = (
                    f'--{_BOUNDARY}\r\n'
                    f'Content-Type: image/jpeg\r\n'
                    f'Content-Length: {len(data)}\r\n\r\n'
                ).encode('ascii')
                self.wfile.write(head)
                self.wfile.write(data)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            return

    # ---- /logs : 日志目录索引页 ----
    def _serve_logs_index(self):
        """列出 logs/ 下所有 log_*.txt + run_*/ 子目录, 顶部一个 "Download all" 按钮.

        viz 活跃时显示禁用态 (服务端 /logs/download 仍会二次校验, 客户端绕过也会被 503).
        run_*/ 是 viz_recorder finalize 产物 (.mp4 + .jpg + 可能的 .err.txt + 未压缩残留),
        .partial 是 ffmpeg 编码中途产物 — 索引页不显示 (避免误导), 下载也跳过.
        """
        logs_dir = _get_logs_dir()
        # ---- log_*.txt 文件 (Asynclogger 产物) ----
        log_files = []
        try:
            for name in os.listdir(logs_dir):
                if not (name.startswith('log_') and name.endswith('.txt')):
                    continue
                p = os.path.join(logs_dir, name)
                if not os.path.isfile(p):
                    continue
                try:
                    st = os.stat(p)
                    log_files.append((name, st.st_size, st.st_mtime))
                except OSError:
                    continue
        except OSError:
            pass
        log_files.sort(key=lambda x: x[2], reverse=True)
        # ---- run_*/ 子目录 (viz_recorder 产物) ----
        run_dirs = []
        try:
            for name in os.listdir(logs_dir):
                if not name.startswith('run_'):
                    continue
                p = os.path.join(logs_dir, name)
                if not os.path.isdir(p):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                # 递归 du; .partial 半成品不计入 (避免显示半成品大小)
                total = 0
                fcount = 0
                for dp, _dn, fns in os.walk(p):
                    for f in fns:
                        if f.endswith('.partial'):
                            continue
                        try:
                            total += os.path.getsize(os.path.join(dp, f))
                            fcount += 1
                        except OSError:
                            pass
                run_dirs.append((name, total, st.st_mtime, fcount))
        except OSError:
            pass
        run_dirs.sort(key=lambda x: x[2], reverse=True)

        busy = _is_viz_busy()
        now = time.time()

        def _age_str(mtime: float) -> str:
            age_s = now - mtime
            if age_s < 60: return f'{int(age_s)}s ago'
            if age_s < 3600: return f'{int(age_s/60)}m ago'
            if age_s < 86400: return f'{int(age_s/3600)}h ago'
            return f'{int(age_s/86400)}d ago'

        log_rows = []
        for name, size, mtime in log_files:
            log_rows.append(
                f'<tr><td>{html.escape(name)}</td>'
                f'<td>{size/(1024.0*1024.0):.2f} MB</td>'
                f'<td>{html.escape(_age_str(mtime))}</td></tr>'
            )
        run_rows = []
        for name, size, mtime, fcount in run_dirs:
            run_rows.append(
                f'<tr><td>{html.escape(name)}</td>'
                f'<td>{fcount}</td>'
                f'<td>{size/(1024.0*1024.0):.2f} MB</td>'
                f'<td>{html.escape(_age_str(mtime))}</td></tr>'
            )

        if busy:
            action_html = (
                '<p style="color:#f88">viz active (frames pushed within '
                f'{int(VIZ_IDLE_THRESHOLD_S)}s) — download disabled. Refresh after detector idles.</p>'
            )
        else:
            action_html = (
                '<p><a class="btn" href="/logs/download">Download all (.tar.gz)</a></p>'
            )

        runs_block = ''
        if run_dirs:
            runs_block = (
                '<h2 style="font-size:14px;margin-top:24px">Run captures</h2>'
                '<table><tr><th>run</th><th>files</th><th>size</th><th>age</th></tr>'
                + ''.join(run_rows)
                + '</table>'
            )

        body_html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>logs</title><style>{_INDEX_CSS}'
            '.btn{display:inline-block;padding:8px 16px;background:#27a;color:#fff;border-radius:4px}'
            'table{border-collapse:collapse;margin-top:8px}'
            'td{padding:4px 16px 4px 0;border-bottom:1px solid #333}'
            'th{padding:4px 16px 4px 0;text-align:left;color:#888;font-weight:normal}'
            'h2{font-size:14px;margin-top:24px;color:#aaa}'
            '</style></head><body>'
            f'<h1>auto_volume_control logs '
            f'({len(log_files)} log files, {len(run_dirs)} run captures)</h1>'
            f'{action_html}'
            '<h2>Daily logs</h2>'
            '<table><tr><th>file</th><th>size</th><th>age</th></tr>'
            + ''.join(log_rows)
            + '</table>'
            + runs_block
            + '<p><a href="/">← back to streams</a></p>'
            + '</body></html>'
        )
        self._serve_simple(200, 'text/html; charset=utf-8', body_html.encode('utf-8'))

    # ---- /logs/download : 流式 tar.gz ----
    def _serve_logs_download(self):
        """把 logs/log_*.txt 全部打包成 tar.gz, 流式写到 wfile.

        约束:
          - viz 活跃时 503 + Retry-After 拒绝 (避免与 MJPEG 编码竞争 CPU/带宽).
          - 不在磁盘留临时档: tarfile 'w|gz' 流式模式直接写到 wfile.
          - 不预知 Content-Length: Connection: close 让客户端按连接关闭判定 body 结束.
        """
        if _is_viz_busy():
            self.send_response(503)
            self.send_header('Retry-After', str(int(VIZ_IDLE_THRESHOLD_S) + 1))
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Connection', 'close')
            msg = (
                f'viz active (frames pushed within {int(VIZ_IDLE_THRESHOLD_S)}s). '
                f'Retry after detector idles.\n'
            ).encode('utf-8')
            self.send_header('Content-Length', str(len(msg)))
            self.end_headers()
            try:
                self.wfile.write(msg)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        logs_dir = _get_logs_dir()
        # 1) log_*.txt 平铺文件 — arcname 直接用 name
        # 2) run_*/ 子目录 — 递归遍历, 跳过 .partial (ffmpeg 编码中途), arcname 带 "run_*/..."
        # tar 内布局: 顶层有 log_*.txt 文件 + run_*/ 目录, 解开后即恢复 logs/ 镜像.
        files = []  # list of (arcname, abs_path)
        try:
            for name in os.listdir(logs_dir):
                p = os.path.join(logs_dir, name)
                if name.startswith('log_') and name.endswith('.txt') and os.path.isfile(p):
                    files.append((name, p))
                    continue
                if name.startswith('run_') and os.path.isdir(p):
                    for dp, _dn, fns in os.walk(p):
                        for f in fns:
                            if f.endswith('.partial'):
                                # 跳过 ffmpeg 编码中途的半成品 — 解开后破碎的 mp4 无用
                                continue
                            full = os.path.join(dp, f)
                            rel = os.path.relpath(full, logs_dir)
                            files.append((rel, full))
        except OSError:
            pass
        # 字典序 — log_YYYYMMDD.txt 时间升序; run_YYYYMMDD_HHMMSS/... 同理
        files.sort()

        archive_name = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Disposition', f'attachment; filename="{archive_name}"')
            self.send_header('Cache-Control', 'no-store')
            # 无 Content-Length → 用 Connection: close 标记 body 结束.
            self.send_header('Connection', 'close')
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        # 'w|gz' = streaming gzip-compressed tar; 写入即时输出,不需要可 seek 的目标.
        # tarfile 不会自己关 fileobj — 我们也不要关 wfile (BaseHTTPRequestHandler 拥有它).
        try:
            with tarfile.open(fileobj=self.wfile, mode='w|gz') as tar:
                for arcname, path in files:
                    try:
                        tar.add(path, arcname=arcname)
                    except OSError:
                        # 单个文件读失败 (e.g. 权限/竞态删除) 不中断整包
                        continue
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.error):
            # 客户端中断: tarfile 写 wfile 失败抛 socket 类异常, 静默退出
            return


# ---------------- HTTP server 生命週期 ----------------

class _QuietServer(ThreadingHTTPServer):
    # 允許地址復用, 避免 stop→start 時 TIME_WAIT 報錯
    allow_reuse_address = True
    # 不因單個客戶端錯誤打出 stack trace (handler 內部已經接住了)
    daemon_threads = True


def start() -> None:
    """按 env 啟動 HTTP server. 若 DEBUG_STREAM_ENABLED 為 false, 直接返回."""
    global _server, _server_thread
    if not _ENABLED:
        return
    if _server is not None:
        return                         # 已啟動, 幂等
    try:
        _server = _QuietServer((_HOST, _PORT), _StreamHandler)
    except OSError as e:
        sys.stderr.write(f'[debug_stream] bind {_HOST}:{_PORT} failed: {e}\n')
        _server = None
        return
    _server_thread = threading.Thread(
        target=_server.serve_forever, name='debug_stream.http', daemon=True,
    )
    _server_thread.start()
    _server_started.set()
    sys.stderr.write(
        f'[debug_stream] listening on http://{_HOST}:{_PORT}/ '
        f'(max_width={_MAX_WIDTH}, quality={_QUALITY})\n'
    )


def stop() -> None:
    """停 server + 所有 encoder. 幂等."""
    global _server, _server_thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
        _server_thread = None
        _server_started.clear()
    # 停所有 encoder
    with _slots_lock:
        for slot in _slots.values():
            slot.shutdown.set()
            # 通知 HTTP handler 和 encoder 醒來退出
            with slot.cond:
                slot.cond.notify_all()
            try:
                slot.raw_queue.put_nowait(None)  # encoder get() 的哨兵
            except queue.Full:
                # 隊列滿, 丟掉舊的再塞哨兵
                try:
                    slot.raw_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    slot.raw_queue.put_nowait(None)
                except queue.Full:
                    pass
        # 不等 encoder 線程 join, 因為是 daemon, 進程退出會連帶清理


def is_enabled() -> bool:
    """HTTP 推流或本地录像任一开启时, 需要生成并编码可视化帧."""
    return _ENABLED or _recorder_hook is not None


# ---------------- 對外主 API: push ----------------

def push(name: str, frame: np.ndarray) -> None:
    """推一幀到指定 stream (非阻塞, 安全, 絕不拋).

    - HTTP 与本地录像都未启用时直接返回; 录像不要求开启 HTTP.
    - 如果 slot 不存在: 惰性創建 + 啟 encoder 線程 (僅首次該 name 時付代價).
    - 如果隊列滿: 丟棄舊幀, 塞入新幀. 永不阻塞檢測主循環.
    - frame 會被 encoder 線程讀取. 如果檢測代碼後續會原地修改此 numpy, 應傳 .copy().
    """
    if not is_enabled():
        return
    if frame is None:
        return
    slot = _get_or_create_slot(name)
    slot.frames_in += 1
    try:
        slot.raw_queue.put_nowait(frame)
    except queue.Full:
        # drop-old: 舊幀讓位給新幀. 兩次 try/except 是為了並發安全 (另一個線程可能剛好把隊列清空)
        try:
            slot.raw_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            slot.raw_queue.put_nowait(frame)
        except queue.Full:
            pass                       # 極端競爭下放棄此幀


# ---------------- 自測: 合成幀推流 (python -m 或 python debug_stream.py) ----------------

def _selftest():
    """獨立自測: 不依賴相機, 推一個循環遞增的合成幀."""
    os.environ.setdefault('DEBUG_STREAM_ENABLED', '1')
    # 重新讀 env (因為模塊級變量已經加載過)
    global _ENABLED
    _ENABLED = True
    start()
    if _server is None:
        sys.stderr.write('[selftest] server failed to start\n')
        return 1
    print(f'open http://localhost:{_PORT}/ in browser; ctrl-C to stop')
    w, h = 1280, 720
    try:
        i = 0
        while True:
            # 灰度漸變 + 幀號字樣
            img = np.zeros((h, w, 3), dtype=np.uint8)
            img[:, :, 0] = (i % 256)
            img[:, :, 1] = ((i // 2) % 256)
            img[:, :, 2] = ((i // 4) % 256)
            cv2.putText(img, f'frame {i}', (40, 80), cv2.FONT_HERSHEY_SIMPLEX,
                        2.0, (255, 255, 255), 3)
            push('selftest', img)
            i += 1
            time.sleep(1.0 / 20.0)     # 20Hz
    except KeyboardInterrupt:
        print('\n[selftest] stopping')
    finally:
        stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(_selftest())
