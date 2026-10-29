#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# capture_single_image.py —— 抓取单帧图像的命令行小工具
# -----------------------------------------------------------------------------
# 用途：
#   用 OpenCamera() 打开相机、连续尝试最多 N 次抓一张合格帧，写到磁盘。
#   适合调试时快速拿一张原始图去离线分析，或在某个时间点截图做参数比对。
# 典型用法：
#   python capture_single_image.py
#   python capture_single_image.py --output ~/tmp/foo.png --max-attempts 5
# =============================================================================

from __future__ import annotations

import argparse
from ctypes import byref, memset, sizeof    # 对 MV_FRAME_OUT 结构体做清零
from datetime import datetime
from pathlib import Path

import cv2

import camera_io


def _default_output_path(output_dir: Path) -> Path:
    """默认输出路径： <output_dir>/frame_YYYYMMDD_HHMMSS.png
    时间戳到秒，保证每次运行都能得到不同文件名。
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"frame_{timestamp}.png"


def capture_one_frame(max_attempts: int = 10):
    """打开相机、抓一张合格帧、关相机、返回 numpy 图。

    - 连续最多 max_attempts 次重试（对偶发丢帧健壮）
    - Mono 原样返回；RGB 会被转成 BGR（方便 cv2.imwrite 保存成常见格式）
    - 任何路径都在 finally 里确保资源释放
    """
    cam = None
    # MV_FRAME_OUT 是 SDK 帧信息结构，先清零
    st_out_frame = camera_io.MV_FRAME_OUT()
    memset(byref(st_out_frame), 0, sizeof(st_out_frame))

    try:
        cam = camera_io.OpenCamera()
        for attempt in range(1, max_attempts + 1):
            result = camera_io.GrabCameraFrame(cam, st_out_frame)
            if result is None:
                # GrabCameraFrame 返回 None 通常代表超时/解码失败；重试
                print(f"[capture] 第 {attempt}/{max_attempts} 次取帧失败，继续重试")
                continue

            numpy_image, n_channel_num = result
            if n_channel_num == 1:
                # 单通道：直接复制（copy 是为了脱离 SDK buffer 的生命周期）
                image = numpy_image.copy()
            else:
                # SDK 输出 RGB，OpenCV 保存/显示习惯是 BGR，这里做一次转换
                image = cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)

            # 释放 SDK 内部的 image buffer，让下一次 Grab 能拿到新帧
            cam.MV_CC_FreeImageBuffer(st_out_frame)
            return image

        # for 走完都没抓到：显式抛异常让调用方知道
        raise RuntimeError(f"连续 {max_attempts} 次取帧失败，未拿到有效图像")
    finally:
        # 异常路径兜底：若 pBufAddr 还指向未释放的数据，这里再释放一次
        if cam is not None and st_out_frame.pBufAddr is not None:
            try:
                cam.MV_CC_FreeImageBuffer(st_out_frame)
            except Exception:
                pass
        if cam is not None:
            camera_io.CloseCamera(cam)


def main() -> int:
    """CLI 入口；返回 0 表示成功。"""
    repo_root = Path(__file__).resolve().parent      # 本文件所在目录
    default_output_dir = repo_root / "single_images"

    parser = argparse.ArgumentParser(description="从 OpenCamera() 采集一帧图像并保存到 single_images")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出文件路径，默认保存到 single_images/frame_时间戳.png",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=10,
        help="最大取帧重试次数，默认 10",
    )
    args = parser.parse_args()

    # expanduser 让 ~ 展开成 $HOME；resolve 取绝对路径避免相对路径歧义
    output_path = args.output.expanduser() if args.output else _default_output_path(default_output_dir)
    output_path = output_path.resolve()
    # 确保父目录存在（parents=True 递归创建）
    output_path.parent.mkdir(parents=True, exist_ok=True)

    image = capture_one_frame(max_attempts=args.max_attempts)
    ok = cv2.imwrite(str(output_path), image)
    if not ok:
        raise RuntimeError(f"保存图片失败: {output_path}")

    print(f"[capture] 已保存: {output_path}")
    return 0


if __name__ == "__main__":
    # SystemExit(code) 把返回值作为进程退出码，shell 脚本可用 $? 判断
    raise SystemExit(main())
