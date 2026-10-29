#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# color_threshold_tuner.py —— 棕色瓶子 RGB 通道+閾值調試工具
# -----------------------------------------------------------------------------
# 用途:
#   給棕色瓶子算法選一個可用的通道 (B/G/R) + 閾值組合.
#   現有 li_det.py 寫死 `img[:,:,1]` (G 通道); 棕色瓶 G 通道幾乎全黑.
#   本工具實時打開相機, 在瀏覽器拖三個 slider (B_thr/G_thr/R_thr), 觀察
#   各通道在不同閾值下的視覺效果. 找到能讓棕色瓶"變透明"
#   (即液面前沿可見) 的通道+閾值, 再回去改 li_det.py.
#
# 用法 (在 x5 上執行, 因為相機掛在 x5):
#   ssh x5
#   cd /home/sunrise/Workspace/auto_volume_control
#   python3 docs/scripts/color_threshold_tuner.py
#   # 然後 Windows 瀏覽器打開: http://192.168.1.194:8090/
#   # 拖 slider, 看四宮格. Ctrl+C 退出.
#
# 通過 docs/scripts/x5.sh 一條龍:
#   bash docs/scripts/x5.sh 'cd auto_volume_control && python3 docs/scripts/color_threshold_tuner.py'
#
# 前提:
#   detector 不能在運行 (相機獨佔). ps -ef | grep main.py 檢查.
#   失敗會看到 0x80000203 (ACCESS_DENIED).
#
# 頁面四宮格:
#   ┌───────────────┬───────────────┐
#   │ BGR 原圖       │ B 通道, <Bthr → 0 │
#   ├───────────────┼───────────────┤
#   │ G 通道, <Gthr → 0│ R 通道, <Rthr → 0 │
#   └───────────────┴───────────────┘
#   每格灰度顯示 (灰度→BGR 三通道相等). 越白 = 該通道越亮.
#   棕色瓶子在 R 通道應該是亮的(透紅), B 通道應該是暗的(吸藍).
#
# 代碼層 (TUNABLES 區) 可改默認值; 也可在頁面 slider 拖.
# =============================================================================

import os
import sys
import time
import threading
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from ctypes import memset, byref, sizeof

# docs/scripts/X.py → 上兩層 = 項目根
import _bootstrap

import numpy as np
import cv2
from bottle_volume_control import config as rt_config
from bottle_volume_control.camera import io as camera_io
from bottle_volume_control.camera.sdk.CameraParams_header import MV_FRAME_OUT

# ────────────────────────── TUNABLES (代碼層默認) ──────────────────────────
PORT = 8090                # 避開 debug_stream 默認 8080
HOST = '0.0.0.0'           # 0.0.0.0 = 監聽所有 NIC, 從 Windows 才連得到
DOWNSCALE = 4              # 顯示時下采樣. 3072×2048 / 4 = 768×512 每格
JPEG_QUALITY = 80          # 80 為畫質/帶寬平衡; 想看細節調高 (90+)
DEFAULT_B_THR = 0
DEFAULT_G_THR = 0
DEFAULT_R_THR = 0
ENCODE_FPS_CAP = 15.0      # MJPEG 上限 (相機 ~14.8fps, 編碼略低於采集即可)
# ─────────────────────────────────────────────────────────────────────────

state_lock = threading.Lock()
state = {'b': DEFAULT_B_THR, 'g': DEFAULT_G_THR, 'r': DEFAULT_R_THR}
latest = {'frame': None, 'ts': 0.0}
stop_flag = threading.Event()


def grab_loop(cam):
    """阻塞循環: 取一幀, 轉 BGR, 寫入 latest. 直到 stop_flag."""
    stOutFrame = MV_FRAME_OUT()
    memset(byref(stOutFrame), 0, sizeof(stOutFrame))
    while not stop_flag.is_set():
        result = camera_io.GrabCameraFrame(cam, stOutFrame)
        if result is None:
            continue
        numpy_image, nch = result
        try:
            if nch == 3:
                bgr = cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)
            else:
                # mono fallback: 三通道復制成 BGR, slider 對 B==G==R 都生效
                bgr = cv2.cvtColor(numpy_image, cv2.COLOR_GRAY2BGR)
        finally:
            cam.MV_CC_FreeImageBuffer(stOutFrame)
        latest['frame'] = bgr
        latest['ts'] = time.time()


def render(bgr, b_thr, g_thr, r_thr):
    """組裝 2×2 全景: BGR | B>=Bthr / G>=Gthr | R>=Rthr."""
    h, w = bgr.shape[:2]
    h2, w2 = max(1, h // DOWNSCALE), max(1, w // DOWNSCALE)
    small = cv2.resize(bgr, (w2, h2), interpolation=cv2.INTER_AREA)
    b, g, r = cv2.split(small)
    b_t = b.copy(); b_t[b < b_thr] = 0
    g_t = g.copy(); g_t[g < g_thr] = 0
    r_t = r.copy(); r_t[r < r_thr] = 0
    b_bgr = cv2.cvtColor(b_t, cv2.COLOR_GRAY2BGR)
    g_bgr = cv2.cvtColor(g_t, cv2.COLOR_GRAY2BGR)
    r_bgr = cv2.cvtColor(r_t, cv2.COLOR_GRAY2BGR)
    fa = (cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
    cv2.putText(small, 'BGR', (10, 30), *fa)
    cv2.putText(b_bgr, f'B>={b_thr}', (10, 30), *fa)
    cv2.putText(g_bgr, f'G>={g_thr}', (10, 30), *fa)
    cv2.putText(r_bgr, f'R>={r_thr}', (10, 30), *fa)
    return cv2.vconcat([cv2.hconcat([small, b_bgr]),
                        cv2.hconcat([g_bgr, r_bgr])])


HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>color threshold tuner</title>
<style>
body{background:#222;color:#ddd;font-family:monospace;margin:8px}
.row{margin:6px 0}
input[type=range]{width:420px;vertical-align:middle}
.val{display:inline-block;min-width:42px;text-align:right;color:#fc0}
img{max-width:100%;border:1px solid #444;display:block;margin-top:6px}
.hint{color:#888;font-size:12px}
</style></head>
<body>
<h3>color threshold tuner — 棕色瓶通道+閾值調試</h3>
<div class="row"><label>B threshold: <span class="val" id="vb">__B__</span></label>
  <input type="range" id="b" min="0" max="255" value="__B__" oninput="upd()"></div>
<div class="row"><label>G threshold: <span class="val" id="vg">__G__</span></label>
  <input type="range" id="g" min="0" max="255" value="__G__" oninput="upd()"></div>
<div class="row"><label>R threshold: <span class="val" id="vr">__R__</span></label>
  <input type="range" id="r" min="0" max="255" value="__R__" oninput="upd()"></div>
<div class="hint">grid = BGR 原圖 / B-thr / G-thr / R-thr (像素 &lt; 閾值 → 0).
棕色瓶子應該在 R 通道明顯亮過 G/B; 拖 slider 找液面在哪個通道+閾值最清晰.</div>
<img src="/stream.mjpg" id="stream">
<script>
function upd(){
  let b=document.getElementById('b').value,
      g=document.getElementById('g').value,
      r=document.getElementById('r').value;
  document.getElementById('vb').textContent=b;
  document.getElementById('vg').textContent=g;
  document.getElementById('vr').textContent=r;
  fetch('/set?b='+b+'&g='+g+'&r='+r);
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_kw):  # 屏蔽默認 stderr 噪音
        pass

    def _send_simple(self, code, body=b'', ctype='text/plain'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ('/', '/index.html'):
            with state_lock:
                cur = dict(state)
            body = (HTML_PAGE
                    .replace('__B__', str(cur['b']))
                    .replace('__G__', str(cur['g']))
                    .replace('__R__', str(cur['r']))
                    ).encode('utf-8')
            self._send_simple(200, body, 'text/html; charset=utf-8')
            return
        if p == '/set':
            qs = parse_qs(u.query)
            with state_lock:
                for k in ('b', 'g', 'r'):
                    if k in qs:
                        try:
                            state[k] = max(0, min(255, int(qs[k][0])))
                        except ValueError:
                            pass
            self._send_simple(204)
            return
        if p == '/stream.mjpg':
            self._serve_mjpeg()
            return
        self._send_simple(404)

    def _serve_mjpeg(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        last_ts = 0.0
        min_dt = 1.0 / max(0.1, ENCODE_FPS_CAP)
        try:
            while not stop_flag.is_set():
                if latest['ts'] <= last_ts:
                    time.sleep(0.02)
                    continue
                now = time.time()
                gap = now - last_ts if last_ts > 0 else min_dt
                if gap < min_dt:
                    time.sleep(min_dt - gap)
                    continue
                frame = latest['frame']
                last_ts = latest['ts']
                if frame is None:
                    time.sleep(0.05)
                    continue
                with state_lock:
                    bt, gt, rt = state['b'], state['g'], state['r']
                composite = render(frame, bt, gt, rt)
                ok, buf = cv2.imencode('.jpg', composite,
                                       [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if not ok:
                    continue
                body = buf.tobytes()
                self.wfile.write(b'--frame\r\n')
                self.wfile.write(b'Content-Type: image/jpeg\r\n')
                self.wfile.write(f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii'))
                self.wfile.write(body)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write(f"[stream] err: {e}\n")


def main():
    print(f"[info] opening camera (exclusive)...")
    cam = camera_io.OpenCamera()
    print(f"[ok] camera up. Open http://<x5-ip>:{PORT}/ in Windows browser.")
    print(f"     defaults: B={state['b']} G={state['g']} R={state['r']}.  Ctrl+C to stop.")

    grab_th = threading.Thread(target=grab_loop, args=(cam,), daemon=True)
    grab_th.start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)

    def _stop(*_):
        stop_flag.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_flag.set()
        grab_th.join(timeout=3.0)
        try:
            camera_io.CloseCamera(cam)
        except Exception as e:
            sys.stderr.write(f"[close] err: {e}\n")
        srv.server_close()
        print("bye.")


if __name__ == '__main__':
    main()
