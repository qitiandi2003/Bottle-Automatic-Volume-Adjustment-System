#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Center-band scale-line detection and tube tracking helpers.

Exports:
  - SlewTubeTracker: tube window tracking with x-boundary slew limits (no y smoothing)
  - center_inside_tube(state, cx, y0, y1): check if image center column is inside tube mask
  - find_center_scale_line(bgr, cx, y0, y1, mask, ...): find one vertical scale line near center
  - refine_scale_edges(bgr, state, x_init, ...): refine left/right edges of the center scale within a narrow band

================================================================================
模块定位：
  本模块负责"试管窗口跟踪 + 中央刻度线定位"，是 detector.py 的核心依赖之一。
  三大职责：
    1) SlewTubeTracker     : 周期性调用 tube.detect_tube_hough_grad 得到试管框；
                             为 x 边界做 slew-rate 限制（单帧最多变 N px），避免
                             一次误检导致窗口跳动；y0/y1 不做平滑。
    2) find_center_scale_line : 在图中央附近窄带里定位最强的"一根"竖直刻度线；
    3) refine_scale_edges     : 在 x_init 附近的窄带里用 Sobel-x 列能量精炼出
                             刻度线的左右沿（亚像素级定位）。
================================================================================
"""

from __future__ import annotations

import argparse
import cv2
import os
import numpy as np
from dataclasses import dataclass
from typing import Optional, Dict, Any

from .tube import detect_tube_hough_grad

# Timing debug switch for tube detection (set env TUBE_DEBUG_TIME=1)

TUBE_DEBUG_TIME = False                     # 硬编码关，想开就改成 True 或接入 env
try:
    # slew 状态调试（每轮打印 prev/new/adj）；用字符串转小写判"非关"语义
    TUBE_DEBUG_SLEW = str(os.getenv('TUBE_DEBUG_SLEW', '0')).strip().lower() not in ('0', 'false', 'no', 'off', '')
except Exception:
    TUBE_DEBUG_SLEW = False


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量。语义和 rt_util._env_flag 一致，但本模块想独立自洽所以重复实现。"""
    v = os.getenv(name)
    if v is None:
        return default
    v = str(v).strip().lower()
    return v not in ('0', 'false', 'no', 'off', '')


def _env_float(name: str, default: float) -> float:
    """读取 float 型环境变量，解析失败回落到 default。"""
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _clamp01(val: float) -> float:
    """把一个数 clamp 到 [0, 1]；非数字输入视作 0。

    公式：  clamp01(x) = max(0, min(1, x))
    """
    try:
        v = float(val)
    except Exception:
        v = 0.0
    return max(0.0, min(1.0, v))


# ---------------- 模块级常量（可由环境变量覆盖） ----------------
# FORCE_TUBE_WINDOW        : 是否强制使用固定窗口（跳过 Hough 检测）
# FORCE_TUBE_LEFT/RIGHT_FRAC : 固定窗口的左右比例（0~1）
# EDGE_DEBUG_VIZ           : 边缘检测调试可视化
FORCE_TUBE_WINDOW_ENV = _env_flag('FORCE_TUBE_WINDOW', True)
FORCE_TUBE_LEFT_FRAC_ENV = _clamp01(_env_float('FORCE_TUBE_LEFT_FRAC', 0.20))
FORCE_TUBE_RIGHT_FRAC_ENV = _clamp01(_env_float('FORCE_TUBE_RIGHT_FRAC', 0.80))
EDGE_DEBUG = _env_flag('EDGE_DEBUG_VIZ', False)


def _edges_from_top_bottom(
    bgr: np.ndarray,
    y0: int,
    y1: int,
    *,
    strip: int = 6,              # 在 y0/y1 附近取多大的水平带做 sobel-y
    quantile: float = 0.7,       # 列能量阈值的分位数
    min_width_px: int = 8,       # 最终跨度必须 >= 8 px 才有效
) -> Optional[tuple[int, int]]:
    """基于试管"上下沿"两条带内的 sobel-y 能量，估计左右 x 边界。

    思路：在 y=y0 和 y=y1 附近各取 ±strip 行的 sobel-y，取列向最大作为列能量；
          用分位数阈值筛出强边缘列；从 mask_cols 找出最长连续段；最后两条带合并。
    """
    h, w = bgr.shape[:2]
    if h <= 0 or w <= 2:
        return None
    # clamp 到合法范围，保证 y0 < y1
    y0 = int(max(0, min(h - 2, y0)))
    y1 = int(max(y0 + 1, min(h - 1, y1)))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gy = np.abs(gy)

    def _span_at(y: int, collect: bool = False):
        """在 y 所在的 ±strip 行里，找出最长的"强边缘列连续段"。"""
        y0s = max(0, y - strip)
        y1s = min(h - 1, y + strip)
        if y1s <= y0s:
            return None, None, (y0s, y1s), None
        band = gy[y0s:y1s + 1]
        col_energy = band.max(axis=0).astype(np.float32)   # 每列取 sobel-y 最大
        nz = col_energy[col_energy > 0]
        if nz.size == 0:
            return None, None, (y0s, y1s), col_energy
        q = float(min(max(quantile, 0.1), 0.95))
        thr = float(np.quantile(nz, q))                     # 分位数作阈值
        if thr <= 0:
            thr = float(nz.mean())                          # 兜底用均值
        mask_cols = col_energy >= thr

        # 在 mask_cols 中找最长连续段，允许有 gap_tol 个 0 做连接
        best = None
        start = None
        gap = 0
        gap_tol = 100
        for idx, val in enumerate(mask_cols):
            if val:
                if start is None:
                    start = idx
                gap = 0
            elif start is not None:
                gap += 1
                if gap > gap_tol:
                    span = (start, idx - gap)
                    if best is None or (span[1] - span[0]) > (best[1] - best[0]):
                        best = span
                    start = None
                    gap = 0
        # 收尾：扫描结束时若还在一段里，补上最后一段
        if start is not None:
            end_idx = mask_cols.size - 1 if gap <= gap_tol else mask_cols.size - gap - 1
            span = (start, max(start, end_idx))
            if best is None or (span[1] - span[0]) > (best[1] - best[0]):
                best = span
        if best is None or (best[1] - best[0] + 1) < int(min_width_px):
            return None, (mask_cols.copy() if collect else None), (y0s, y1s), col_energy
        span = (int(best[0]), int(best[1]))
        return span, (mask_cols.copy() if collect else None), (y0s, y1s), col_energy

    span_top, mask_top, bounds_top, energy_top = _span_at(y0, collect=EDGE_DEBUG)
    span_bot, mask_bot, bounds_bot, energy_bot = _span_at(y1, collect=EDGE_DEBUG)
    # 合并两条带：两边都有时取并集；只有一边时直接用它
    spans = [s for s in (span_top, span_bot) if s is not None]
    if not spans:
        return None
    if len(spans) == 1:
        left, right = spans[0]
    else:
        left = min(s[0] for s in spans)
        right = max(s[1] for s in spans)
        if (right - left + 1) < int(min_width_px):
            # 合并后反而太窄 → 用两条里较宽那条
            widest = max(spans, key=lambda sp: sp[1] - sp[0])
            left, right = widest
    left = int(max(0, min(w - 2, left)))
    right = int(max(left + 1, min(w - 1, right)))

    if EDGE_DEBUG:
        # 可视化：把两条带画出来 + 最宽段的矩形 + 最终 (left, right) 的蓝框
        try:
            vis = cv2.cvtColor(gray.copy(), cv2.COLOR_GRAY2BGR)
            for (span, mask_cols, bounds, color, energy) in (
                (span_top, mask_top, bounds_top, (0, 200, 0), energy_top),
                (span_bot, mask_bot, bounds_bot, (0, 0, 255), energy_bot),
            ):
                if bounds is None:
                    continue
                y0s, y1s = bounds
                cv2.rectangle(vis, (0, y0s), (w - 1, y1s), color, 1)
                if mask_cols is not None:
                    cols = np.where(mask_cols)[0]
                    if cols.size:
                        vis[y0s:y1s + 1, cols] = color
                if span is not None:
                    cv2.rectangle(vis, (span[0], y0s), (span[1], y1s), (0, 255, 255), 2)
                if energy is not None and energy.size:
                    idx_max = int(np.argmax(energy))
                    cv2.line(vis, (idx_max, y0s), (idx_max, y1s), (255, 255, 255), 1)
            cv2.rectangle(vis, (left, y0), (right, y1), (255, 0, 0), 2)
            # 如果图太宽，缩放到 600 px 以内再 imshow（否则看起来太大）
            scale = min(1.0, 600.0 / max(1, w))
            if scale < 1.0:
                vis = cv2.resize(vis, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            cv2.imshow('edge_debug', vis)
            cv2.waitKey(1)
        except Exception:
            pass

    return left, right


def _estimate_lr_edges(bgr: np.ndarray, y0: int, y1: int, mask: Optional[np.ndarray] = None,
                       *, smooth_ksize: int = 21, quantile: float = 0.55,
                       min_width_px: int = 6) -> Optional[tuple[int, int]]:
    """从 [y0..y1] 范围内的 Sobel-x 列能量估计左右边界。

    算法：
      ① 切出 [y0..y1] 的灰度 ROI
      ② Sobel-x 绝对值按列求均值 → 列能量 energy
      ③ 若给了 mask，把每列 mask.sum 为 0 的列能量置 0
      ④ 高斯平滑
      ⑤ 分位数取阈值 → 取 thr 以上的列
      ⑥ 首个/最后一个 → (left, right)
      ⑦ 若宽度不够，用左右 1/4 里的 argmax 做兜底
    """
    h, w = bgr.shape[:2]
    if h == 0 or w == 0:
        return None
    y0 = int(max(0, min(h - 2, y0)))
    y1 = int(max(y0 + 1, min(h - 1, y1)))
    roi = cv2.cvtColor(bgr[y0:y1 + 1], cv2.COLOR_BGR2GRAY)
    sobx = cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)
    energy = np.abs(sobx).mean(axis=0).astype(np.float32)
    if mask is not None:
        try:
            mroi = mask[y0:y1 + 1]
            # 每列是否至少有 1 个 mask 像素 → 无效列乘 0
            valid = (mroi.sum(axis=0) > 0).astype(np.float32)
            energy *= valid
        except Exception:
            pass
    if float(energy.max()) <= 1e-6:
        return None
    k = int(max(3, smooth_ksize) | 1)           # ksize 强制奇数
    try:
        kernel = cv2.getGaussianKernel(k, 0)
        energy = cv2.filter2D(energy.reshape(-1, 1), -1, kernel).ravel()
    except Exception:
        pass
    nz = energy[energy > 0]
    if nz.size == 0:
        return None
    q = float(min(max(quantile, 0.1), 0.95))
    thr = float(np.quantile(nz, q))
    if thr <= 0:
        thr = float(nz.mean())
    idx = np.where(energy >= thr)[0]
    if idx.size >= 2:
        left = int(idx[0])
        right = int(idx[-1])
        if (right - left) >= int(min_width_px):
            return left, right
    # fallback: take global maxima from both sides
    # 兜底方案：左右各取 1/4 宽度范围内的 argmax
    span = max(4, int(len(energy) // 4))
    left = int(np.argmax(energy[:span]))
    right_offset = int(np.argmax(energy[max(0, len(energy) - span):]))
    right = int(min(len(energy) - 1, len(energy) - span + right_offset))
    if right - left >= int(min_width_px):
        return left, right
    return None


@dataclass
class TubeState:
    """试管几何状态快照（纯数据类）。
    ok=False 表示当前未检测到试管；其它字段可能都是 None。
    """
    ok: bool = False
    y0: Optional[int] = None       # 上管壁 y
    y1: Optional[int] = None       # 下管壁 y
    x_left: Optional[int] = None   # 试管左边界 x
    x_right: Optional[int] = None  # 试管右边界 x
    mask: Optional[np.ndarray] = None   # 二值掩膜 (H,W)
    viz: Optional[np.ndarray] = None    # 可视化图（make_viz=True 时填充）
    # binarization intermediates from detect_tube_hough_grad — exposed for debug viz
    # (TODO.md Pipeline binarization map: #1 edges_otsu, #2 edges_closed); ROI-local strips, 0/255 uint8
    edges_otsu: Optional[np.ndarray] = None
    edges_closed: Optional[np.ndarray] = None


class SlewTubeTracker:
    """
    Track tube window using periodic detection + x-boundary slew limits.
    - y0/y1: not smoothed; use direct detection values
    - x_left/x_right: asymmetric slew limiting (cam_tube style)

    为什么要 slew limit：
        偶尔一次 Hough 误检会把 x_left/x_right 抖动到离谱的位置，让后续的
        "中心是否在试管内"、"刻度线搜索带"一并出错。限制单帧 x 最多向内/向外
        移动多少像素，可大幅抑制这种偶发抖动。
    """

    def __init__(self, *, update_every: int = 10,
                 y_low: float = 0.30, y_high: float = 0.90,
                 left_inc_max: int = 10, right_dec_max: int = 10,
                 margin: int = 3,
                 force_fixed: Optional[bool] = None,
                 fixed_left_frac: Optional[float] = None,
                 fixed_right_frac: Optional[float] = None) -> None:
        """
        update_every      : 每多少帧真正跑一次检测，其他帧复用旧 state；默认 10
        y_low / y_high    : 检测窗口占图高的上下比例
        left_inc_max      : x_left 单帧最多向内收几 px（左边界变大是内收）
        right_dec_max     : x_right 单帧最多向内收几 px（右边界变小是内收）
        margin            : 供外部使用的额外 y 边距
        force_fixed       : 是否强制固定窗口（跳过 Hough）
        fixed_left/right_frac : 固定窗口的左右比例
        """
        self.update_every = int(max(1, update_every))
        self.y_low = float(y_low)
        self.y_high = float(y_high)
        self.left_inc_max = int(max(0, left_inc_max))
        self.right_dec_max = int(max(0, right_dec_max))
        self.margin = int(max(0, margin))
        self._state = TubeState(ok=False)
        self._frame_count = 0
        self._prev_xl: Optional[int] = None      # 上次的 x_left（用于 slew 比较）
        self._prev_xr: Optional[int] = None
        # 下面的 "... if X is None else X" 是"允许构造时覆盖默认值"的惯用写法
        self.force_fixed = FORCE_TUBE_WINDOW_ENV if force_fixed is None else bool(force_fixed)
        fl = FORCE_TUBE_LEFT_FRAC_ENV if fixed_left_frac is None else fixed_left_frac
        fr = FORCE_TUBE_RIGHT_FRAC_ENV if fixed_right_frac is None else fixed_right_frac
        self.fixed_left_frac = _clamp01(fl)
        self.fixed_right_frac = _clamp01(fr)
        # 防止 left >= right 导致窗口宽度 0
        if self.fixed_left_frac >= self.fixed_right_frac:
            # keep at least 5% width to avoid degenerate window
            eps = 0.05
            self.fixed_right_frac = min(1.0, max(self.fixed_left_frac + eps, self.fixed_right_frac))
            if self.fixed_right_frac > 1.0:
                self.fixed_right_frac = 1.0
                self.fixed_left_frac = max(0.0, self.fixed_right_frac - eps)

    @property
    def state(self) -> TubeState:
        """对外只读访问当前 state。"""
        return self._state

    def update(self, bgr: np.ndarray, *, make_viz: bool = False, gray_full=None) -> TubeState:
        """每帧调用一次。只有"本帧需要刷新"时才真正运行检测。

        是否刷新的规则：
            - 每 update_every 帧刷新一次；
            - 或者当前 state 无效（ok=False）时强制刷新。

        gray_full: POST-MERGE 共享 trunk gray (full frame BGR2GRAY); 透传给 detect_tube_hough_grad
                   省掉它内部的 cvtColor. None=旧行为.
        """
        self._frame_count += 1
        need_update = (self._frame_count % self.update_every) == 0 or not self._state.ok
        if not need_update:
            return self._state

        h, w = bgr.shape[:2]
        detection: Optional[Dict[str, Any]] = None
        if self.force_fixed:
            # 固定窗口模式：直接按比例算出 y0/y1/xl/xr 并合成 detection
            detection = self._build_fixed_detection(h, w)
        else:
            try:
                detection = detect_tube_hough_grad(
                    bgr,
                    y_low_ratio=self.y_low,
                    y_high_ratio=self.y_high,
                    debug_timing=TUBE_DEBUG_TIME,
                    gray_full=gray_full,
                )
            except Exception:
                self._state = TubeState(ok=False)
                return self._state

        st = self._apply_detection_result(bgr, detection, make_viz=make_viz)
        self._state = st
        return self._state

    def _apply_detection_result(self, bgr: np.ndarray, detection: Dict[str, Any], *, make_viz: bool) -> TubeState:
        """把 detection 结果应用到 state 上，同时做 slew 限幅。"""
        h, w = bgr.shape[:2]
        # clamp 到合法范围并保证 y0 < y1, xl < xr
        y0 = int(max(0, min(h - 2, detection.get('y0', 0))))
        y1 = int(max(y0 + 1, min(h - 1, detection.get('y1', h - 1))))
        xl_new = int(max(0, min(w - 2, detection.get('x_left', 0))))
        xr_new = int(max(xl_new + 1, min(w - 1, detection.get('x_right', w - 1))))
        mask = detection.get('mask')
        # 没有 mask 或 mask 形状不对 → 用 y0..y1 x xl..xr 的矩形代替
        if mask is None or mask.shape[:2] != (h, w):
            mask = np.zeros((h, w), np.uint8)
            mask[y0:y1 + 1, xl_new:xr_new + 1] = 255

        # 第一次没 prev_xl/xr，用当前值作为基线
        if self._prev_xl is None or self._prev_xr is None:
            self._prev_xl, self._prev_xr = xl_new, xr_new

        # x_left 的 slew 限幅：
        #   - 向外扩（xl_new 比 prev 更小）→ 立即接受（试管变宽需要及时跟上）
        #   - 向内收（xl_new 比 prev 更大）→ 最多 left_inc_max px/帧
        if xl_new <= self._prev_xl:
            xl_adj = xl_new
        else:
            xl_adj = min(self._prev_xl + self.left_inc_max, xl_new)

        # x_right 对称处理：
        #   - 向外扩（xr_new 比 prev 更大）→ 立即接受
        #   - 向内收（xr_new 比 prev 更小）→ 最多 right_dec_max px/帧
        if xr_new >= self._prev_xr:
            xr_adj = xr_new
        else:
            xr_adj = max(self._prev_xr - self.right_dec_max, xr_new)

        if TUBE_DEBUG_SLEW:
            try:
                dl = xl_new - self._prev_xl
                dr = xr_new - self._prev_xr
                print(f"[tube-slew] prev=({self._prev_xl},{self._prev_xr}) new=({xl_new},{xr_new}) adj=({xl_adj},{xr_adj}) dl={dl} dr={dr} limits=({self.left_inc_max},{self.right_dec_max})")
            except Exception:
                pass

        # 最终 clamp + 保证 xl < xr
        xl_adj = max(0, min(xr_adj - 1, xl_adj))
        xr_adj = min(w - 1, max(xl_adj + 1, xr_adj))

        self._prev_xl, self._prev_xr = xl_adj, xr_adj

        # ensure mask respects current x bounds
        # slew 后 mask 的 x 范围可能已偏离，直接把 xl_adj 外侧掩膜抹掉
        try:
            mask[:, :xl_adj] = 0
            mask[:, xr_adj + 1:] = 0
        except Exception:
            pass

        st = TubeState(
            ok=True, y0=y0, y1=y1, x_left=xl_adj, x_right=xr_adj, mask=mask, viz=None,
            edges_otsu=detection.get('edges_otsu'),
            edges_closed=detection.get('edges_closed'),
        )

        if make_viz:
            # 画可视化：半透明红色掩膜 + 黄色 y 线 + 蓝色 x 线 + 紫色行排除线
            base = bgr.copy()
            overlay = base.copy()
            m = mask.copy()
            overlay[m > 0] = (0, 0, 255)
            viz = cv2.addWeighted(overlay, 0.4, base, 0.6, 0)
            cv2.line(viz, (0, y0), (w - 1, y0), (0, 255, 255), 2)
            cv2.line(viz, (0, y1), (w - 1, y1), (0, 255, 255), 2)
            cv2.line(viz, (xl_adj, 0), (xl_adj, h - 1), (255, 0, 0), 2)
            cv2.line(viz, (xr_adj, 0), (xr_adj, h - 1), (255, 0, 0), 2)
            try:
                # 若 detection 带了 row_exclude（屏蔽液面带），也画出来
                re = detection.get('row_exclude')
                if not re:
                    re = detection.get('center_exclude')
                if re and len(re) == 2:
                    y0_re, y1_re = int(re[0]), int(re[1])
                    y0_re = max(0, min(h - 1, y0_re))
                    y1_re = max(0, min(h - 1, y1_re))
                    if y1_re > y0_re:
                        cv2.line(viz, (0, y0_re), (w - 1, y0_re), (255, 0, 255), 1)
                        cv2.line(viz, (0, y1_re), (w - 1, y1_re), (255, 0, 255), 1)
            except Exception:
                pass
            st.viz = viz
        return st

    def _build_fixed_detection(self, h: int, w: int) -> Dict[str, Any]:
        """固定窗口 detection：按 y_low/y_high/fixed_*_frac 生成一个矩形 detection。"""
        y0 = int(round(h * self.y_low))
        y1 = int(round(h * self.y_high))
        y0 = max(0, min(h - 2, y0))
        y1 = max(y0 + 1, min(h - 1, y1))
        xl = int(round(w * self.fixed_left_frac))
        xr = int(round(w * self.fixed_right_frac))
        xl = max(0, min(w - 2, xl))
        xr = max(xl + 1, min(w - 1, xr))
        mask = np.zeros((h, w), np.uint8)
        mask[y0:y1 + 1, xl:xr + 1] = 255
        return {"y0": y0, "y1": y1, "x_left": xl, "x_right": xr, "mask": mask}


def center_inside_tube(state: TubeState, cx: int, y0: int, y1: int) -> bool:
    """判断图像中心列 cx 是否"穿过"试管掩膜。

    用途：确认"中心刻度线"位置确实在试管范围内；否则说明试管偏置了，
    接下来的中央刻度检测不可靠。

    实现：取 mask[y0..y1, cx] 这一列，只要有任意非零像素就算在试管内。
    """
    try:
        if not state or not state.ok:
            return False
        if state.mask is None:
            return False
        h, w = state.mask.shape[:2]
        cx = int(max(0, min(w - 1, cx)))
        y0 = int(max(0, min(h - 1, y0)))
        y1 = int(max(0, min(h - 1, y1)))
        if y1 < y0:
            y0, y1 = y1, y0
        col = state.mask[y0:y1 + 1, cx]
        return bool(np.count_nonzero(col) > 0)
    except Exception:
        return False


def _clahe_gray(g: np.ndarray) -> np.ndarray:
    """对单通道图做 CLAHE 局部对比度增强；失败就原样返回。"""
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        g = clahe.apply(g)
    except Exception:
        pass
    return g


def find_center_scale_line(
    bgr: np.ndarray,
    cx: int,
    y0: int,
    y1: int,
    mask: Optional[np.ndarray] = None,
    *,
    band: int = 50,              # 在 cx±band 的水平带内搜刻度线
    lambda_center: float = 2.5,  # 中心吸引系数：越大越偏好离 cx 近的候选
    conf_min: float = 0.15,       # 置信度下限（该列 thr 以上像素占比）
    score_min: Optional[float] = None,   # 综合分数下限；None 表示不限制
    viz: bool = False,
    base_viz: Optional[np.ndarray] = None,  # if given, draw overlay on base_viz.copy() instead of bgr.copy()
    gray_full: Optional[np.ndarray] = None,  # POST-MERGE 共享 trunk gray; 提供则跳过内部 cvtColor
) -> Dict[str, Any]:
    """
    In a vertical window y0..y1 and horizontal band [cx-band, cx+band],
    find at most ONE narrow vertical line (scale mark). Returns dict:
      {found, x, score, conf, vis}

    评分公式：
        col[x]      = sum_y |Sobel_x(y,x)|        (列能量)
        penalty[x]  = lambda_center * |x - cx|    (离中心越远惩罚越大)
        score[x]    = col[x] - penalty[x]
        best_x      = argmax score

    置信度：
        thr  = max(10, 0.6 * Otsu_thr(col_vec))
        conf = mean(col_vec > thr)
    """
    h, w = bgr.shape[:2]
    cx = int(cx)
    band = int(max(1, band))
    # clamp 搜索带
    x0 = int(max(0, min(w - 1, cx - band)))
    x1 = int(max(0, min(w - 1, cx + band)))
    if x1 < x0:
        x0, x1 = x1, x0
    y0 = int(max(0, min(h - 1, y0)))
    y1 = int(max(0, min(h - 1, y1)))
    if y1 < y0:
        y0, y1 = y1, y0

    if gray_full is not None:
        # 直接吃 trunk gray; CLAHE+blur 不会就地修改 (clahe.apply / GaussianBlur 都返回新数组),
        # 因此不会影响其它 branch 复用 gray_full. (TODO.md Pipeline binarization map: #4)
        gray = gray_full
    else:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = _clahe_gray(gray)
    gray = cv2.medianBlur(gray, 5) #中值滤波去细小液珠噪点
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    bg = cv2.GaussianBlur(gray, (61, 1), 0)
    gray2 = cv2.subtract(bg, gray)
    gray2 = cv2.absdiff(gray, bg)

    gx = cv2.Sobel(gray2, cv2.CV_16S, 1, 0, ksize=3)
    gx = cv2.convertScaleAbs(gx)               

    # ROI 内的 Sobel-x 图；若有 mask，把 mask=0 的位置置 0
    roi = gx[y0:y1 + 1, x0:x1 + 1].copy()
    if mask is not None:
        mroi = mask[y0:y1 + 1, x0:x1 + 1]
        roi[mroi == 0] = 0

    # column-wise score
    # 每列求和 → 列能量 float32
    col = roi.sum(axis=0).astype(np.float32)

    # center penalty
    # |x - cx| * lambda_center；xs 用全局 x 坐标，保证与 cx 直接可减
    xs = np.arange(x0, x1 + 1, dtype=np.float32)
    penalty = float(lambda_center) * np.abs(xs - float(cx))
    score = col - penalty

    k = int(np.argmax(score)) if score.size > 0 else 0
    best_score = float(score[k]) if score.size > 0 else 0.0
    best_x = int(x0 + k)

    # confidence along the column
    # 取 best_x 这一列的所有像素，看有多少 > 动态阈值
    col_vec = gx[y0:y1 + 1, best_x]
    # dynamic threshold from Otsu fraction
    try:
        # 用 Otsu 阈值的 60% 作为"显著"阈值；至少 10
        t, _ = cv2.threshold(col_vec, 0, 255, cv2.THRESH_OTSU)
        thr = max(10.0, 0.35 * float(t))
    except Exception:
        thr = 20.0
    conf = float((col_vec > thr).mean()) if col_vec.size > 0 else 0.0

    ok = True
    if score_min is not None and best_score < float(score_min):
        ok = False
    if conf < float(conf_min):
        ok = False

    out = {
        'found': bool(ok),
        'x': int(best_x) if ok else None,
        'score': float(best_score),
        'conf': float(conf),
        'vis': None,
        # FULL uint8 Sobel-x intermediate exposed for binary viz (TODO.md Pipeline binarization map: #4)
        'gx': gx,
        # search-band coords for downstream viz crop / reference lines
        'cx': int(cx),
        'band_x0': int(x0),
        'band_x1': int(x1),
        'y_win0': int(y0),
        'y_win1': int(y1),
    }

    if viz:
        vis = (base_viz.copy() if base_viz is not None else bgr.copy())
        # center line
        # 白色：图像中线 cx
        cx_line = int(cx)
        cv2.line(vis, (cx_line, 0), (cx_line, h - 1), (255, 255, 255), 1)
        # band lines
        # 青色：搜索带左右边
        cv2.line(vis, (x0, 0), (x0, h - 1), (200, 200, 0), 1)
        cv2.line(vis, (x1, 0), (x1, h - 1), (200, 200, 0), 1)
        # y window
        # 绿色：y 窗口
        cv2.line(vis, (0, y0), (w - 1, y0), (0, 255, 0), 1)
        cv2.line(vis, (0, y1), (w - 1, y1), (0, 255, 0), 1)
        # detected line
        # 黄色（命中）或灰色（未命中）：检测到的刻度线
        cv2.line(vis, (best_x, y0), (best_x, y1), (0, 255, 255) if ok else (180, 180, 180), 2)
        # annotate offset
        dx = int(best_x - cx)
        txt = f"dx={dx:+d}px conf={conf:.2f} score={best_score:.0f}"
        cv2.putText(vis, txt, (8, max(20, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (40, 200, 40) if ok else (0, 0, 255), 2, cv2.LINE_AA)
        out['vis'] = vis

    return out


def _render_col_profile_image(vals, *, width=800, height=200, vcolor=(60, 180, 255), anchor=None):
    """把一维列能量 vals 渲染成折线图（800×200）。

    和 det_utils 里同名内部函数几乎一致，区别：
      - 这里的 anchor 是"相对 n-1 的下标"（本函数坐标系），
      - det_utils._render_col_profile_image_local 的 anchor 是全局 x 再映射。
    """
    import numpy as np, cv2
    a = np.asarray(vals, np.float32).ravel()
    n = int(a.size)
    img = np.full((height, width, 3), 255, np.uint8)
    if n <= 1:
        return img
    vmin, vmax = float(a.min()), float(a.max())
    if vmax <= vmin + 1e-12:
        vmax = vmin + 1.0
    an = (a - vmin) / (vmax - vmin)              # 归一化到 [0,1]
    # 线性插值把 n 个样本插到 width 个像素上
    xs_src = np.linspace(0, n - 1, num=n, dtype=np.float32)
    xs_dst = np.linspace(0, n - 1, num=width, dtype=np.float32)
    as_ = np.interp(xs_dst, xs_src, an)
    # 画 25/50/75% 参考横线
    for y in [0.25, 0.5, 0.75]:
        yy = int(height - 1 - y * (height - 1))
        cv2.line(img, (0, yy), (width - 1, yy), (230, 230, 230), 1)
    ys = (height - 1 - (as_ * (height - 1))).astype(np.int32)
    xs = np.arange(width, dtype=np.int32)
    cv2.polylines(img, [np.stack([xs, ys], 1)], False, vcolor, 2)
    # anchor：在 vals 下标空间画红色竖线
    if isinstance(anchor, (int, float)) and anchor is not None:
        xx = int(np.clip(round(float(anchor) / max(1, n - 1) * (width - 1)), 0, width - 1))
        cv2.line(img, (xx, 0), (xx, height - 1), (255, 0, 0), 2)
    return img


def refine_scale_edges(
    bgr,
    state,
    x_init: int,
    *,
    band: int = 13,           # 分析带半宽：clamp 到 [5,15]
    y_margin: int = 3,       # 上下各收多少 y 边距
    min_width_px: int = 3,   # 最终宽度至少 N 像素
    use_mask: bool = False,  # 是否用 mask 加权
    smooth_ksize: int = 9,   # 平滑核大小，clamp 到 [7,11] 且强制奇数
    make_debug: bool = True, # 是否生成 overlay/profile 调试图
    gray_full=None,          # POST-MERGE 共享 trunk gray; None=旧行为 (内部 cvtColor)
):
    """
    Refine the left/right edges of the center scale around a coarse x_init.
    - Computes Sobel-x column energy in a narrow band [x_init-band, x_init+band]
    - Optionally weights by tube mask inside [y0+margin, y1-margin] (disabled by default)
    - Returns dict {ok, x_left, x_right, x_center, width, x0, x1, y0, y1, col_energy, overlay, profile}

    用途：中央刻度线已由 find_center_scale_line 给出粗 x_init，现在要算它的"左右沿"，
    以便计算刻度线的真实宽度和中心（亚像素级）。detector.py 会用 width 作为
    GapStability 输入，用 x_center 作为刻度锁定位置。
    """
    import numpy as np, cv2
    out = {
        'ok': False,
        'x_left': None,
        'x_right': None,
        'x_center': None,
        'width': None,
        'x0': None,
        'x1': None,
        'y0': None,
        'y1': None,
        'col_energy': None,
        'overlay': None,
        'profile': None,
    }
    try:
        h, w = bgr.shape[:2]
        # 从 state 里拿 y 范围，再内收 y_margin
        y0 = max(0, min(h - 1, int(getattr(state, 'y0', 0)) + int(y_margin)))
        y1 = max(0, min(h - 1, int(getattr(state, 'y1', h - 1)) - int(y_margin)))
        if y1 <= y0:
            return out
        xi = int(np.clip(int(x_init), 0, w - 1))
        band = int(np.clip(int(band), 5, 15))
        x0 = int(max(0, xi - band))
        x1 = int(min(w - 1, xi + band))
        if x1 <= x0:
            return out

        if gray_full is not None:
            # POST-MERGE [D]: 跳过内部 cvtColor; refine 不做 CLAHE (旧行为同), 直接 blur+Sobel
            g = gray_full
        else:
            g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        sobx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
        sobx = np.abs(sobx)
        roi = sobx[y0:y1 + 1, x0:x1 + 1]

        # 是否用 mask 加权列能量
        if use_mask and getattr(state, 'mask', None) is not None:
            try:
                m = (state.mask[y0:y1 + 1, x0:x1 + 1] > 0).astype(np.float32)
                # 公式：col_energy[x] = sum_y (sobx[y,x]*m[y,x]) / max(1, sum_y m[y,x])
                col_sum = (roi * m).sum(axis=0)
                col_len = np.maximum(1.0, m.sum(axis=0))
                col_energy = (col_sum / col_len).astype(np.float32)
            except Exception:
                col_energy = roi.mean(axis=0).astype(np.float32)
        else:
            col_energy = roi.mean(axis=0).astype(np.float32)

        # smooth to reduce jaggies
        try:
            ksize = int(smooth_ksize)
            if ksize < 3:
                ksize = 3
            if (ksize % 2) == 0:
                ksize += 1
            ksize = max(7, min(11, ksize))
            k = cv2.getGaussianKernel(ksize, 0)
            col_energy = cv2.filter2D(col_energy.reshape(-1, 1), -1, k).ravel()
        except Exception:
            pass

        xs_local = np.arange(col_energy.size, dtype=np.int32)
        cut = int(np.clip(int(xi - x0), 0, col_energy.size - 1))
        if col_energy.size < 2:
            return out

        # find local maxima (including endpoints as fallback)
        # 局部极大 + 首末端点作为兜底
        loc = np.where(
            (col_energy[1:-1] >= col_energy[:-2]) &
            (col_energy[1:-1] >= col_energy[2:])
        )[0] + 1
        peaks = np.unique(
            np.concatenate([
                loc,
                np.array([0, col_energy.size - 1], dtype=np.int32)
            ])
        )
        # 所有峰按能量降序排序，取前两个作为左右沿
        if peaks.size < 2:
            order = np.argsort(col_energy)[::-1]
        else:
            order = peaks[np.argsort(col_energy[peaks])[::-1]]

        if peaks.size >= 2:
            best_two = order[:2]
        else:
            best_two = order[:2]

        # 两个峰里较小下标 = 左沿，较大下标 = 右沿
        xl_rel = int(np.clip(np.sort(best_two)[0], 0, col_energy.size - 1))
        xr_rel = int(np.clip(np.sort(best_two)[-1], 0, col_energy.size - 1))
        # 若左右重合，向右/向左挪 1
        if xl_rel == xr_rel:
            if xr_rel + 1 < col_energy.size:
                xr_rel += 1
            elif xl_rel - 1 >= 0:
                xl_rel -= 1

        xl_idx = xl_rel
        xr_idx = max(xr_rel, xl_idx + 1)
        x_left = int(x0 + xl_idx)
        x_right = int(x0 + xr_idx)
        width = int(x_right - x_left)
        # 宽度校验 + 兜底
        if width < int(min_width_px):
            # try a tiny nudge to the right if degenerate
            # 退化情况：把 x_right 往 xi+1 推
            x_right = int(min(w - 1, max(x_right, xi + 1)))
            width = int(x_right - x_left)
        if width < int(min_width_px):
            # 第二次兜底：强制把右沿推到 xi + min_width_px
            x_left = int(max(0, min(x_left, xi)))
            x_right = int(min(w - 1, max(x_right, xi + int(min_width_px))))
            width = int(x_right - x_left)
        if width < int(min_width_px):
            # 实在不行就失败
            return out

        # 刻度中心 = 左右沿的中点
        x_center = int(round(0.5 * (x_left + x_right)))
        out.update({
            'ok': True,
            'x_left': x_left,
            'x_right': x_right,
            'x_center': x_center,
            'width': width,
            'x0': x0,
            'x1': x1,
            'y0': y0,
            'y1': y1,
            'col_energy': col_energy,
        })

        if make_debug:
            # overlay lines on the original image (or tracker viz if present)
            # 叠加画图：左右沿黄色、中心绿色 + 文字注释
            try:
                base = getattr(state, 'viz', None)
                if base is None:
                    base = bgr
                vis = base.copy()
            except Exception:
                vis = bgr.copy()
            cv2.line(vis, (x_left, y0), (x_left, y1), (0, 255, 255), 2)
            cv2.line(vis, (x_right, y0), (x_right, y1), (0, 255, 255), 2)
            cv2.line(vis, (x_center, y0), (x_center, y1), (0, 200, 0), 1)
            try:
                txt = f"scale refine: x0={x0} x1={x1} xi={xi} L={x_left} R={x_right} w={width}"
                cv2.putText(vis, txt, (8, max(20, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 200, 40), 2, cv2.LINE_AA)
            except Exception:
                pass
            out['overlay'] = vis
            try:
                # profile 图：列能量折线 + 左峰（橙）+ 右峰（绿）+ xi 锚（红）
                profile = _render_col_profile_image(col_energy, anchor=(xi - x0))
                n = max(1, col_energy.size - 1)
                for idx, color in [
                    (int(x_left - x0), (0, 140, 255)),   # 左峰：橙色
                    (int(x_right - x0), (0, 200, 0)),    # 右峰：绿色
                ]:
                    xx = int(np.clip(round(float(idx) / n * (profile.shape[1] - 1)), 0, profile.shape[1] - 1))
                    cv2.line(profile, (xx, 0), (xx, profile.shape[0] - 1), color, 2)
                out['profile'] = profile
            except Exception:
                pass
        return out
    except Exception:
        return out


def _tube_state_from_detection(det: Dict[str, Any]) -> TubeState:
    """把 detection 字典转成 TubeState 数据类；给 CLI main() 用。"""
    return TubeState(
        ok=True,
        y0=int(det.get('y0', 0)),
        y1=int(det.get('y1', 0)),
        x_left=int(det.get('x_left', 0)),
        x_right=int(det.get('x_right', 0)),
        mask=det.get('mask'),
        viz=det.get('viz'),
    )


def main():
    """CLI：读一张图 → detect_tube_hough_grad → refine_scale_edges → 打印 + 写调试图。"""
    parser = argparse.ArgumentParser(description="Refine center scale edges using Sobel column energy.")
    parser.add_argument("--image", required=True, help="Input BGR image path.")
    parser.add_argument("--cx", type=int, default=None, help="Initial x guess; defaults to tube mid-span.")
    parser.add_argument("--band", type=int, default=9, help="Half-width of horizontal band in pixels (clamped to 8-10).")
    parser.add_argument("--smooth-ksize", type=int, default=9, help="1D Gaussian smoothing kernel size (odd, clamped to 7-11).")
    parser.add_argument("--y-margin", type=int, default=3, help="Vertical margin shaved from tube window before refinement.")
    parser.add_argument("--min-width", type=int, default=3, help="Minimum allowed scale width in pixels.")
    parser.add_argument("--use-mask", action="store_true", help="Enable mask-weighted column energy.")
    parser.add_argument("--no-debug", action="store_true", help="Disable overlay/profile generation.")
    parser.add_argument("--out-dir", default=None, help="Directory to dump overlay/profile images when debug is enabled.")
    parser.add_argument("--tube-low", type=float, default=0.30, help="Lower ratio for tube detection window.")
    parser.add_argument("--tube-high", type=float, default=0.90, help="Upper ratio for tube detection window.")
    args = parser.parse_args()

    img = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"Failed to read image: {args.image}")

    try:
        det = detect_tube_hough_grad(img, y_low_ratio=args.tube_low, y_high_ratio=args.tube_high)
    except Exception as exc:
        raise SystemExit(f"Tube detection failed: {exc}")

    state = _tube_state_from_detection(det)
    # 默认初始 x = (x_left + x_right) / 2
    if state.x_left is not None and state.x_right is not None:
        cx_guess = int(round(0.5 * (state.x_left + state.x_right)))
    else:
        cx_guess = img.shape[1] // 2
    cx = int(args.cx) if args.cx is not None else cx_guess

    result = refine_scale_edges(
        img,
        state,
        cx,
        band=args.band,
        y_margin=args.y_margin,
        min_width_px=args.min_width,
        use_mask=args.use_mask,
        smooth_ksize=args.smooth_ksize,
        make_debug=not args.no_debug,
    )

    print(f"Tube window: y0={state.y0} y1={state.y1} x_left={state.x_left} x_right={state.x_right}")
    if result.get("ok"):
        print("Refine success: "
              f"x_left={result['x_left']} x_right={result['x_right']} "
              f"x_center={result['x_center']} width={result['width']}")
    else:
        print("Refine failed.")

    if args.no_debug or args.out_dir is None or not result.get("ok"):
        return

    # 落盘 overlay / profile 两张图
    os.makedirs(args.out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.image))[0]
    overlay = result.get("overlay")
    if overlay is not None:
        cv2.imwrite(os.path.join(args.out_dir, f"{stem}_refine_overlay.png"), overlay)
    profile = result.get("profile")
    if profile is not None:
        cv2.imwrite(os.path.join(args.out_dir, f"{stem}_refine_profile.png"), profile)


if __name__ == "__main__":
    main()
