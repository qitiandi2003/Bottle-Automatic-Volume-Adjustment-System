# -*- coding: utf-8 -*-
# =============================================================================
# framework/algo —— 共享算法 trunk
# -----------------------------------------------------------------------------
# 暴露 FrameTree + prepare_frame: 单帧的共享 gray_full, 让 center_scale / auto_exposure
# 等模块通过可选的 gray_full= kwarg 复用同一份 BGR2GRAY 结果, 省掉多分支独立 cvtColor.
#
# 设计原则: 这里 *不* re-wrap 算法函数. 算法继续住在 center_scale / li_det / auto_exposure,
# 只是 *接受* 可选的 gray_full=; 调用方传 ctx.tree.gray_full 即可.
# =============================================================================
from .tree import FrameTree, prepare_frame

__all__ = ["FrameTree", "prepare_frame"]
