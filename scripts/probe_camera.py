#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# probe_camera.py —— 打開相機一次, 打印所有關鍵參數然後關閉
# -----------------------------------------------------------------------------
# 用途: 配置核對. 單次運行, 列出 Width/Height/FrameRate/Exposure/Gain/PixelFormat.
# 典型用法:
#   python scripts/probe_camera.py
#
# 支持已安裝的 bottle_volume_control 包，也支持開發目錄的共用 bootstrap。
# 前提: 相機未被其它進程佔用 (檢測用 ps -ef | grep main.py).
# =============================================================================
import argparse

import _bootstrap  # noqa: F401  shared source-checkout bootstrap
from bottle_volume_control.camera.sdk.CameraParams_header import (
    MVCC_INTVALUE, MVCC_FLOATVALUE, MVCC_ENUMVALUE,
)


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="打开海康相机并显示关键参数")
    parser.parse_args(argv)
    from bottle_volume_control.camera import io as camera_io

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
