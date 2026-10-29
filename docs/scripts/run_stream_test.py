#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# run_stream_test.py —— debug_stream 的 e2e 煙霧測試
# -----------------------------------------------------------------------------
# 用途:
#   啟動 Detector 跑一段時間, SIGTERM/SIGINT 收到後乾淨停機. 搭配 run_stream_test.sh
#   可以驗證: detector 構造 → debug_stream.start → detector 線程推幀 → HTTP 服務能 curl 到 JPEG.
#
# 獨立使用:
#   DEBUG_STREAM_ENABLED=1 python3 docs/scripts/run_stream_test.py
#   # 另開終端:
#   curl http://localhost:8080/ | head
#   # 想停就 ctrl-C 或 kill -TERM
# =============================================================================
import os
import signal
import sys
import time

# docs/scripts/run_stream_test.py → 上兩層是項目根
_here = os.path.dirname(os.path.abspath(__file__))
_proj = os.path.abspath(os.path.join(_here, '..', '..'))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

import camera_io
from detector import Detector

stop_flag = {'stop': False}


def _on_signal(*_args):
    stop_flag['stop'] = True


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)

cam = camera_io.OpenCamera()
det = None
try:
    det = Detector(cam)
    det.start()
    print("[test] detector running; SIGTERM/SIGINT to stop", flush=True)
    while not stop_flag['stop']:
        time.sleep(0.5)
    print("[test] stopping", flush=True)
finally:
    if det is not None:
        try:
            det.stop()
        except Exception as e:
            print(f"[test] det.stop err: {e}")
    try:
        camera_io.CloseCamera(cam)
    except Exception as e:
        print(f"[test] CloseCamera err: {e}")
print("[test] done", flush=True)
