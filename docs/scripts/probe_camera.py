#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# probe_camera.py —— 打開相機一次, 打印所有關鍵參數然後關閉
# -----------------------------------------------------------------------------
# 用途: 配置核對. 單次運行, 列出 Width/Height/FrameRate/Exposure/Gain/PixelFormat.
# 典型用法:
#   cd /home/sunrise/Workspace/auto_volume_control
#   python3 docs/scripts/probe_camera.py
#
# 也可以直接 python3 任意位置, 腳本會自動從 __file__ 算出項目根加入 sys.path.
# 前提: 相機未被其它進程佔用 (檢測用 ps -ef | grep main.py).
# =============================================================================
import os
import sys

# docs/scripts/probe_camera.py → 上兩層是項目根
_here = os.path.dirname(os.path.abspath(__file__))
_proj = os.path.abspath(os.path.join(_here, '..', '..'))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

import rt_config  # noqa: F401  side effect: adds MvImport to sys.path
import camera_io
from MvCameraControl_class import MVCC_INTVALUE, MVCC_FLOATVALUE, MVCC_ENUMVALUE


def q_int(cam, name):
    v = MVCC_INTVALUE()
    ret = cam.MV_CC_GetIntValue(name, v)
    return v.nCurValue if ret == 0 else f"ERR 0x{ret:x}"


def q_float(cam, name):
    v = MVCC_FLOATVALUE()
    ret = cam.MV_CC_GetFloatValue(name, v)
    return v.fCurValue if ret == 0 else f"ERR 0x{ret:x}"


def q_enum(cam, name):
    v = MVCC_ENUMVALUE()
    ret = cam.MV_CC_GetEnumValue(name, v)
    return f"0x{v.nCurValue:x}" if ret == 0 else f"ERR 0x{ret:x}"


def main():
    cam = camera_io.OpenCamera()
    try:
        print(f"Width (current):       {q_int(cam, 'Width')}")
        print(f"Height (current):      {q_int(cam, 'Height')}")
        print(f"WidthMax (sensor):     {q_int(cam, 'WidthMax')}")
        print(f"HeightMax (sensor):    {q_int(cam, 'HeightMax')}")
        print(f"AcquisitionFrameRate:  {q_float(cam, 'AcquisitionFrameRate')}")
        print(f"ResultingFrameRate:    {q_float(cam, 'ResultingFrameRate')}")
        print(f"ExposureTime (us):     {q_float(cam, 'ExposureTime')}")
        print(f"Gain:                  {q_float(cam, 'Gain')}")
        print(f"PixelFormat enum:      {q_enum(cam, 'PixelFormat')}")
    finally:
        camera_io.CloseCamera(cam)


if __name__ == '__main__':
    main()
