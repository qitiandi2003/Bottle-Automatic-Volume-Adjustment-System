# -*- coding: utf-8 -*-
# =============================================================================
# framework/algo/tree.py —— 共享 trunk gray_full (二值化树根节点)
# -----------------------------------------------------------------------------
# Pipeline 每帧前置一次 BGR2GRAY → ctx.tree.gray_full, 让 [A]/[B]/[C]/[D] 4 个 phase/action
# 分支共用一份, 替代各自独立调 cvtColor (~3-5ms/次, 4 次合并节省 ~10ms/帧).
#
# 调用方:
#   ctx.tree = prepare_frame(bgr)       # Pipeline 主循环每帧前置
#   handler.handle(frame, ctx):
#       gray_full = ctx.tree.gray_full
#       res = find_center_scale_line(bgr, ..., gray_full=gray_full)
# =============================================================================
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FrameTree:
    """单帧的共享 trunk. handlers / actions 只读, 下一帧 prepare_frame 重新构造 (老实例 GC)."""
    bgr: np.ndarray
    gray_full: np.ndarray

    @property
    def shape(self) -> "tuple[int, int]":
        return self.bgr.shape[:2]


def prepare_frame(bgr: np.ndarray) -> FrameTree:
    """构造一帧的共享 trunk. cvtColor 1× (~3-5ms)."""
    gray_full = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return FrameTree(bgr=bgr, gray_full=gray_full)
