# -*- coding: utf-8 -*-
# =============================================================================
# framework/camera.py —— Camera 上下文管理器 (薄封装 camera_io.*)
# -----------------------------------------------------------------------------
# camera_io.OpenCamera/CloseCamera/GrabCameraFrame 是函数式 API. 这层把它们封进
# __enter__/__exit__, 一个 `with Camera() as camera:` 管完生命周期.
#
# grab() 返回 BGR uint8 numpy (RGB 输入自动转, mono 直通转 3-ch); 失败返回 None.
# release_frame() 在主循环处理完一帧后调用, 把 SDK buffer 还回 pool.
# =============================================================================
from __future__ import annotations

from typing import Any, Optional

import cv2
import numpy as np

# rt_config 副作用: 把 MvImport 路径加进 sys.path. 在这里 import 是安全的 — 它本身
# 不依赖 MVCAM_COMMON_RUNENV. camera_io / MvCameraControl_class 改成 __enter__
# 时再 import, 让本模块在没装 SDK 的开发机上也可被 import (单元测试 / static 检查).
import rt_config  # noqa: F401


class Camera:
    """相机上下文管理器. 用法:

        with Camera() as cam:
            while running:
                bgr = cam.grab()
                if bgr is None: continue
                ...

    线程模型: 单线程使用. SDK 本身允许多线程 grab 但本系统只有 Pipeline 主循环抓帧.
    """

    def __init__(self) -> None:
        self._cam: Any = None
        self._st_out: Any = None
        self._camera_io: Any = None  # 缓存 camera_io 模块引用, 避免 grab() 路径多次 import

    # ---- 上下文管理协议 ----

    def __enter__(self) -> "Camera":
        # 这里才真正触发 SDK 加载. Orin 镜像 (无 SDK) 不会在 import 阶段炸,
        # 而是在调用 with Camera() 时报错 — 那是预期的行为.
        from ctypes import byref, memset, sizeof  # noqa: WPS433
        import camera_io  # noqa: WPS433
        from MvCameraControl_class import MV_FRAME_OUT  # noqa: WPS433

        self._camera_io = camera_io
        self._cam = camera_io.OpenCamera()
        st = MV_FRAME_OUT()
        memset(byref(st), 0, sizeof(st))
        self._st_out = st
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._cam is None or self._camera_io is None:
            return
        try:
            self._camera_io.CloseCamera(self._cam)
        finally:
            self._cam = None
            self._st_out = None
            self._camera_io = None

    # ---- 抓帧 ----

    def grab(self) -> Optional[np.ndarray]:
        """取一帧 BGR uint8 numpy. 失败返回 None.

        实现:
          - GrabCameraFrame 返回 (numpy, nChannelNum) 或 None;
          - nChannelNum==3 → 输入 RGB; cv2.cvtColor → BGR;
          - nChannelNum==1 → mono 转 3-ch BGR (下游 prepare_frame 假设 3-ch);
          - **不**立即 MV_CC_FreeImageBuffer — 调用方处理完一帧后调 release_frame() 释放.

        为什么 Free 时机要在处理之后:
          SDK 内部有 buffer pool (~5 buffers). 相机填一个 buffer → user GetImageBuffer 取走 →
          user FreeImageBuffer 还回 → 相机继续填. 如果 grab() 立即 Free, 相机在 user 处理期间
          (~70-100ms) 可以用满整个 pool 填新帧, 队列累积 → 下次 grab 拿到的是几帧之前的 buffer
          → 视觉延迟. Free 推迟到处理完, 处理期间 user 占住 1 个 buffer, 相机 pool 减 1 →
          缓解累积.
        """
        if self._cam is None or self._st_out is None or self._camera_io is None:
            return None
        result = self._camera_io.GrabCameraFrame(self._cam, self._st_out)
        if result is None:
            return None
        numpy_image, n = result
        # **不**在这里 Free; 留给 release_frame() (Pipeline 主循环尾部调用).
        # numpy_image 是 numpy.frombuffer 包装函数内部新分配的 DstBuffer (camera_io.py:307,322-330),
        # 不是包 stOutFrame.pBufAddr, 所以 stOutFrame 的 Free 时机 *不* 影响 numpy_image 内容有效性.
        # 但 stOutFrame 持有的相机 SDK buffer 必须及时还回 pool, 才能让相机有空间填新帧.
        if n == 3:
            return cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)
        if n == 1:
            # mono 路径: SDK 返回 (H,W) 单通道. 下游 (prepare_frame BGR2GRAY / li_det bgr[:,:,1] /
            # auto_exposure 内部 cvtColor 等) 都假设 3-ch BGR, 1-ch 进去 cvtColor 直接炸.
            # 这里一律转 3-ch BGR (R=G=B=gray); BGR2GRAY 对 R==G==B 输入返回 = R, 数值一致.
            return cv2.cvtColor(numpy_image, cv2.COLOR_GRAY2BGR)
        return numpy_image

    def release_frame(self) -> None:
        """主循环处理完一帧后调用; 把相机 SDK 持有的 buffer 还回 pool.

        no-throw: 即使 stOutFrame 已经被 free 过 / cam 已 close, 这里也吃掉异常.
        """
        if self._cam is None or self._st_out is None:
            return
        try:
            self._cam.MV_CC_FreeImageBuffer(self._st_out)
        except Exception:
            pass
