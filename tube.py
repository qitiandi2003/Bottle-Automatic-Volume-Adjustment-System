#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Detect the volumetric-flask tube section strictly inside a vertical window
(y_low_ratio ~ y_high_ratio), using vertical gradient + HoughLinesP to find
two near-horizontal, near-parallel edges (top/bottom of the tube).
Outputs:
  - *_mask.png : binary mask of the tube, hard-clipped to the window
  - *_viz.png  : overlay visualization (yellow=window, blue=detected edges,
                 red=mask, thin blue lines show x-span)

==============================================================================
模块作用：
    专注于"横放的定容瓶试管段"几何检测：给定一张相机图，在指定的垂直窗口
    (y_low_ratio, y_high_ratio) 内，用 Sobel-y + HoughLinesP 找到两条近乎
    水平、相互近平行的直线（试管上沿 + 下沿），构造试管轮廓掩膜，并给出
    可视化图。center_scale.py 会调用 detect_tube_hough_grad 作为其几何输入。
==============================================================================
"""

import os
import math                        # atan2 算直线斜率角度用
import argparse                    # CLI 参数
import time                        # 时序调试
from pathlib import Path

import cv2
import numpy as np


# -------------------------------
# Utility
# -------------------------------
def makedirs(p: Path) -> None:
    """Path.mkdir 的 parents+exist_ok 封装：一把梭创建目录树，已存在不报错。"""
    p.mkdir(parents=True, exist_ok=True)


def draw_window_lines(img, y0, y1, color=(0, 255, 255), thickness=2):
    """在图上画两条水平黄线，表示有效检测窗口 [y0, y1]。"""
    # 从 x=0 到 x=w-1 画满图宽
    cv2.line(img, (0, y0), (img.shape[1] - 1, y0), color, thickness)
    cv2.line(img, (0, y1), (img.shape[1] - 1, y1), color, thickness)


def y_on_line(x, x1, y1, x2, y2):
    """给定两点 (x1,y1),(x2,y2) 决定的直线, 求该直线在 x 处的 y 值。

    数学表达：    y = y1 + (y2 - y1) * (x - x1) / (x2 - x1)
    当 x2==x1（竖直线）时退化，返回 y1。
    """
    if x2 == x1:
        return y1
    return y1 + (y2 - y1) * (x - x1) / float(x2 - x1)


# -------------------------------
# Core detector (Hough + vertical grad)
# -------------------------------
def detect_tube_hough_grad(
    img_bgr: np.ndarray,
    y_low_ratio: float = 0.30,      # 有效窗口下界（占图高比例）
    y_high_ratio: float = 0.90,     # 有效窗口上界
    angle_tol_deg: float = 10.0,    # 允许的最大水平偏角
    min_len_frac: float = 0.15,     # 最短线段长度占图宽比例
    hough_thresh: int = 50,         # Hough 累加阈值
    max_gap: int = 20,              # 同一直线可合并的最大间隔
    border_guard: int = 20,         # 离 ROI 顶/底边至少多少像素
    retry_once: bool = True,        # 失败时是否再放宽参数重试一次
    debug_timing: bool = False,     # 是否打印各步骤耗时
    gray_full: "np.ndarray | None" = None,  # POST-MERGE 共享 trunk gray (full frame); None=旧路径自己 cvtColor
):
    """
    Returns a dict with:
      mask, viz, y0, y1, x_left, x_right, tube_height_median, tube_height_mad
    Or raises RuntimeError with a readable reason.

    返回字典：
        mask                 : 全图大小的二值掩膜 (0/255)
        viz                  : 可视化叠加图
        y0, y1               : 检测窗口的 y 边界（图坐标）
        x_left, x_right      : 试管段的左/右 x 边界
        tube_height_median   : 沿 x 方向试管上下边线之间距离的中位数（px）
        tube_height_mad      : 对应的中位绝对偏差 MAD，表征高度抖动幅度
    """
    h, w = img_bgr.shape[:2]
    if not (0 < y_low_ratio < y_high_ratio < 1):
        # 比例必须严格单调在 (0,1) 内，否则切 ROI 出异常
        raise RuntimeError("y ratios must satisfy 0 < low < high < 1")

    # 依据比例计算实际 y 边界；round 后再 int，避免浮点误差带来的 off-by-one
    y0 = int(round(h * y_low_ratio))
    y1 = int(round(h * y_high_ratio))
    roi = img_bgr[y0:y1].copy()    # 切出子图作为后续处理对象
    # POST-MERGE [A]: 提前对 gray_full 做同样的 y 切片. _run_once 内闭包用. None 时走旧路径 cvtColor(roi).
    gray_roi_pre = gray_full[y0:y1].copy() if gray_full is not None else None

    # timing helpers —— 小型计时器：一段段测耗时
    start_ts = time.perf_counter()
    last_ts = start_ts
    def _pt(label: str):
        """打印当前阶段耗时及累计耗时；debug_timing=False 时直接返回。"""
        nonlocal last_ts
        if not debug_timing:
            return
        now = time.perf_counter()
        step_ms = (now - last_ts) * 1000.0
        total_ms = (now - start_ts) * 1000.0
        print(f"[tube-timing] {label}: {step_ms:.1f} ms (total {total_ms:.1f} ms)")
        last_ts = now

    if debug_timing:
        print(f"[tube-timing] ROI: w={roi.shape[1]} h={roi.shape[0]} (y0={y0}, y1={y1})")

    def _run_once(ang_tol, min_len_f, hth, mgap):
        """单次完整检测，供主体和失败重试复用。参数是可调的宽严度。"""
        # 1) Preprocess (grayscale + CLAHE + slight blur)
        if gray_roi_pre is not None:
            # POST-MERGE [A]: 复用 trunk gray (slice). 旧路径的 cvtColor(roi) 跳过.
            gray = gray_roi_pre
        else:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        # CLAHE 用于局部对比度增强，clipLimit=2.0 控制极端放大，tile 8x8 分块
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        g = clahe.apply(gray)
        g = cv2.GaussianBlur(g, (3, 3), 0)     # 3x3 轻微降噪
        _pt("preprocess (gray+clahe+blur)")

        # 2) Vertical gradient -> emphasize horizontal edges
        # Sobel(0,1) 就是 dI/dy：大 = 横向边缘 → 瓶壁上下沿
        gy = cv2.Sobel(g, cv2.CV_16S, 0, 1, ksize=3)
        gy = cv2.convertScaleAbs(gy)           # |gy| 并压回 uint8
        # Otsu threshold on vertical gradient —— 自动寻找灰度阈值二值化
        _, edges_otsu = cv2.threshold(gy, 0, 255, cv2.THRESH_OTSU)
        # Morphological close to connect fragmented horizontal edges
        # 核 9x3：横宽竖窄，目的是把断开的水平边连成长线
        try:
            k = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
            edges = cv2.morphologyEx(edges_otsu, cv2.MORPH_CLOSE, k, iterations=1)
        except Exception:
            edges = edges_otsu
        _pt("grad+otsu+morph")

        # 3) HoughLinesP for long, near-horizontal lines
        # minLineLength：至少 min_len 像素的线段才算数；过短的噪声自动淘汰
        min_len = max(40, int(min_len_f * w))
        # rho=1, theta=1°；threshold=hth（累加阈值）；maxLineGap=mgap（允许合并的断点间距）
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=hth,
                                minLineLength=min_len, maxLineGap=mgap)
        if lines is None:
            raise RuntimeError("no lines")
        if debug_timing:
            try:
                print(f"[tube-timing] hough lines: n={int(lines.size/4)} thr={hth} min_len={min_len} gap={mgap}")
            except Exception:
                print(f"[tube-timing] hough lines: n=? thr={hth} min_len={min_len} gap={mgap}")
        _pt("hough")

        # 4) Filter near-horizontal, away from top/bottom border
        cands = []
        # lines.reshape(-1,4) 把 (N,1,4) 转成 (N,4) 便于解包
        for (x1, y1r, x2, y2r) in lines.reshape(-1, 4):
            dx, dy = (x2 - x1), (y2r - y1r)
            if dx == 0:
                # 垂直线跳过（我们只要横向的）
                continue
            # 直线与水平方向的夹角（度），atan2(dy,dx) ∈ (-π,π]，取绝对值
            angle = abs(math.degrees(math.atan2(dy, dx)))
            if angle > ang_tol:
                continue                         # 倾斜过大 → 不算"近水平"
            ym = (y1r + y2r) / 2.0
            # 离 ROI 上下边 border_guard 像素内的线不可信（常是图边界伪影）
            if ym < border_guard or ym > (roi.shape[0] - border_guard):
                continue
            length = (dx * dx + dy * dy) ** 0.5  # 线段长度
            cands.append((x1, y1r, x2, y2r, angle, length, ym))
        if debug_timing:
            print(f"[tube-timing] horiz cand: n={len(cands)} (ang_tol={ang_tol})")
        _pt("filter horiz cands")

        if not cands:
            raise RuntimeError("no near-horizontal lines")

        # 5) Split to upper/lower by ROI mid-y
        # 以 ROI 中线 midy 为界，y<midy 的归上半部候选，>= midy 归下半部
        midy = roi.shape[0] / 2.0
        # 各取长度最长的 Top6（reverse=True 降序）
        upper = sorted([c for c in cands if c[6] < midy], key=lambda t: t[5], reverse=True)[:6]
        lower = sorted([c for c in cands if c[6] >= midy], key=lambda t: t[5], reverse=True)[:6]
        if not upper or not lower:
            raise RuntimeError("failed to find both top & bottom lines")
        if debug_timing:
            print(f"[tube-timing] split: upper={len(upper)} lower={len(lower)} midy={midy:.1f}")
        _pt("split upper/lower")

        # 6) Choose the best pair: most parallel + largest x-overlap
        #    Additionally constrain vertical separation between top/bottom
        #    edges: min > 5% of ROI height and max < 40% of ROI height.
        best, best_score = None, -1e9
        combos = 0
        # 穷举 upper×lower 的每种组合（最多 6×6 = 36 对）
        for tu in upper:
            for bl in lower:
                combos += 1
                x1t, y1t, x2t, y2t, angt, len_t, _ = tu
                x1b, y1b, x2b, y2b, angb, len_b, _ = bl
                ang_diff = abs(angt - angb)
                if ang_diff > ang_tol:  # stronger parallelism
                    continue              # 两条线不够平行 → 跳过
                # 两段的 x 起止（不保证 x1<x2，先 sort）
                xt0, xt1 = sorted([x1t, x2t])
                xb0, xb1 = sorted([x1b, x2b])
                # x 方向重叠宽度：overlap = min(右端) - max(左端)
                overlap = min(xt1, xb1) - max(xt0, xb0)
                if overlap < min_len * 0.5:
                    continue              # 重叠太短 → 不是同一段管壁
                # Constrain the vertical separation between the two lines.
                # Evaluate heights across the overlapped x-range in ROI coords.
                xL, xR = max(xt0, xb0), min(xt1, xb1)
                # Sample every 2 px to keep it light.
                # 在重叠区内每 2 px 采一个点做高度统计，节省计算
                xs_pair = np.arange(xL, xR + 1, 2)
                if xs_pair.size == 0:
                    continue
                ht_vals = []
                for _x in xs_pair:
                    yt = y_on_line(_x, x1t, y1t, x2t, y2t)
                    yb = y_on_line(_x, x1b, y1b, x2b, y2b)
                    ht_vals.append(abs(yb - yt))     # 两线在同一 x 的 y 差 = 高度
                ht_vals = np.asarray(ht_vals, dtype=np.float32)
                roi_h = float(roi.shape[0])
                min_allowed = 0.05 * roi_h            # 最小允许高度 = ROI 高的 5%
                max_allowed = 0.40 * roi_h            # 最大允许高度 = ROI 高的 40%
                if not (ht_vals.min() > min_allowed and ht_vals.max() < max_allowed):
                    continue                          # 高度分布越界 → 非试管
                # 评分公式：
                #   score = overlap - 40 * ang_diff + 0.01 * (len_t + len_b)
                # 语义：重叠越长、角度越一致、线段越长 → 越好
                # 40 这个系数是经验值，让 1° 角差抵掉 40 px 的重叠优势
                score = overlap - 40 * ang_diff + 0.01 * (len_t + len_b)
                if score > best_score:
                    best_score = score
                    best = (tu, bl, max(xt0, xb0), min(xt1, xb1))
        if debug_timing:
            print(f"[tube-timing] pair scoring: combos={combos} best_score={best_score:.1f}")
        _pt("pair scoring")

        if best is None:
            raise RuntimeError("no good parallel pair with overlap")

        # 解开最佳组合：两条直线参数 + 公共 x 范围
        (x1t, y1t, x2t, y2t, _, _, _), (x1b, y1b, x2b, y2b, _, _, _), x_left_roi, x_right_roi = best

        # 7) Build mask polygon between two lines (in full-image coords)
        # 沿 x 方向每 1 像素取一个点；顶线从左到右，底线从右到左 —— 构成封闭多边形
        xs = np.arange(x_left_roi, x_right_roi + 1, 1)
        # 注意这里加 y0：y_on_line 在 ROI 坐标系，+y0 回到整图坐标系
        top_pts = np.array([[x, int(y_on_line(x, x1t, y1t, x2t, y2t) + y0)] for x in xs], dtype=np.int32)
        bot_pts = np.array([[x, int(y_on_line(x, x1b, y1b, x2b, y2b) + y0)] for x in xs[::-1]], dtype=np.int32)

        mask = np.zeros((h, w), dtype=np.uint8)
        # 多边形格式要求 (N,1,2)，所以 reshape
        poly = np.vstack([top_pts, bot_pts]).reshape((-1, 1, 2))
        cv2.fillPoly(mask, [poly], 255)
        _pt("build mask")

        # Hard-clip outside the window
        # 窗口外的掩膜强行清零，保证掩膜不会泄漏到 y0 以上或 y1 以下
        mask[:y0] = 0
        mask[y1:] = 0

        # Visualization
        viz = img_bgr.copy()
        draw_window_lines(viz, y0, y1)            # 黄色窗口线
        # 蓝色的顶/底两条线（注意 BGR 下 (255,0,0) 是蓝）
        cv2.line(viz, (x1t, y1t + y0), (x2t, y2t + y0), (255, 0, 0), 2)
        cv2.line(viz, (x1b, y1b + y0), (x2b, y2b + y0), (255, 0, 0), 2)
        # 左右两条 x 边界（细蓝线）
        cv2.line(viz, (x_left_roi, 0), (x_left_roi, h - 1), (255, 0, 0), 1)
        cv2.line(viz, (x_right_roi, 0), (x_right_roi, h - 1), (255, 0, 0), 1)
        # 把掩膜区覆上红色半透明
        overlay = viz.copy()
        overlay[mask > 0] = (0, 0, 255)
        # addWeighted: out = overlay*0.4 + viz*0.6 —— 40% 红色蒙版 + 60% 原图
        viz = cv2.addWeighted(overlay, 0.4, viz, 0.6, 0)
        _pt("viz overlay")

        # Tube height stats along xs —— 再次沿 xs 算一遍高度统计
        heights = []
        for x in xs:
            yt = y_on_line(x, x1t, y1t, x2t, y2t)
            yb = y_on_line(x, x1b, y1b, x2b, y2b)
            heights.append(abs(yb - yt))
        heights = np.asarray(heights, dtype=np.float32)
        med_h = float(np.median(heights))                # 高度中位数
        # MAD = Median Absolute Deviation —— 比标准差更抗异常值
        mad_h = float(np.median(np.abs(heights - med_h)))
        _pt("heights stats")

        res = {
            "mask": mask,
            "viz": viz,
            "y0": y0,
            "y1": y1,
            "x_left": int(x_left_roi),
            "x_right": int(x_right_roi),
            "tube_height_median": med_h,
            "tube_height_mad": mad_h,
            # binarization intermediates exposed for debug viz (TODO.md Pipeline binarization map: #1, #2)
            # both are ROI-local (y0..y1 strip), 0/255 uint8
            "edges_otsu": edges_otsu,    # #1 OTSU(|Sobel-y|) — pre-morph
            "edges_closed": edges,       # #2 after MORPH_CLOSE 9x3 — what HoughLinesP actually consumed
        }
        _pt("done")
        return res

    # run once; if fail, retry with looser params once
    try:
        return _run_once(angle_tol_deg, min_len_frac, hough_thresh, max_gap)
    except RuntimeError as e:
        if not retry_once:
            raise
        # slightly looser
        try:
            if debug_timing:
                print(f"[tube-timing] retry with looser params due to: {e}")
            # 放宽参数：
            #   ang_tol    +4°     (最多 14°)
            #   min_len_f  -0.03   (最少 0.12)
            #   hth        -15     (最少 35)
            #   mgap       +8
            return _run_once(
                ang_tol=min(14.0, angle_tol_deg + 4.0),
                min_len_f=max(0.12, min_len_frac - 0.03),
                hth=max(35, hough_thresh - 15),
                mgap=max_gap + 8,
            )
        except RuntimeError as e2:
            # 两次都失败，抛带有两次失败原因的综合错误
            raise RuntimeError(f"Detector failed: {e} / {e2}")


# -------------------------------
# CLI
# -------------------------------
def main():
    """CLI 入口：读入一张图，写出 *_mask.png 和 *_viz.png 到指定目录。"""
    parser = argparse.ArgumentParser(
        description="Detect the tube section strictly inside a vertical window."
    )
    parser.add_argument("--img", default="/home/b5090/yolo/截图 2025-10-20 14-40-05.png", type=str, help="Path to input image")
    parser.add_argument("--out", default="out_tube", type=str, help="Output directory")
    parser.add_argument("--y-low", default=0.30, type=float, help="Lower ratio on Y axis (0~1)")
    parser.add_argument("--y-high", default=0.90, type=float, help="Upper ratio on Y axis (0~1)")
    parser.add_argument("--no-retry", action="store_true", help="Disable one-time retry with looser params")
    args = parser.parse_args()

    img_path = Path(args.img)
    out_dir = Path(args.out)
    makedirs(out_dir)

    img = cv2.imread(str(img_path))
    if img is None:
        raise SystemExit(f"Cannot read image: {img_path}")

    try:
        res = detect_tube_hough_grad(
            img,
            y_low_ratio=args.y_low,
            y_high_ratio=args.y_high,
            retry_once=(not args.no_retry),
        )
    except RuntimeError as e:
        # 出错以非零码退出，便于脚本集成
        raise SystemExit(f"[FAIL] {e}")

    # Save outputs
    base = out_dir / (img_path.stem)             # 同名无扩展 + _mask.png / _viz.png
    p_mask = str(base) + "_mask.png"
    p_viz = str(base) + "_viz.png"
    cv2.imwrite(p_mask, res["mask"])
    cv2.imwrite(p_viz, res["viz"])

    # Report
    print(
        "[OK] y0=%d y1=%d | x_left=%d x_right=%d | tube_height_median=%.1f (MAD=%.1f)\n"
        "     mask=%s\n     viz =%s"
        % (
            res["y0"],
            res["y1"],
            res["x_left"],
            res["x_right"],
            res["tube_height_median"],
            res["tube_height_mad"],
            p_mask,
            p_viz,
        )
    )


if __name__ == "__main__":
    main()
