# -*- coding: utf-8 -*-
# =============================================================================
# framework/viz.py —— 可视化生成
# -----------------------------------------------------------------------------
# 5 块可视化 helper, 全部 free function (不依赖 self), 失败返回 None (调用方用
# if x is not None 守护).
#
# 输出 5 个 push key (debug_stream MJPEG 推流):
#
#   ┌─────────────────────────┬───────────────────────────────────────────┐
#   │ key                     │ phase / 调用点                            │
#   ├─────────────────────────┼───────────────────────────────────────────┤
#   │ vial_overlay            │ SearchTube/LockScale: st.viz 直接 push    │
#   │                         │ Dosing:                make_vial_overlay_locked │
#   │ tube_edges_binary       │ LockScale + Dosing:    make_tube_edges_viz       │
#   │ scale_sobel_x_binary    │ LockScale (需 res['gx']): make_scale_sobel_x_viz  │
#   │ liquid_signal_panel     │ Dosing (需 update_liquid_level signals):           │
#   │                         │                        make_liquid_signal_panel   │
#   │ scale_refine_profile    │ FreezeScale action 直接 push refine 输出的 profile  │
#   └─────────────────────────┴───────────────────────────────────────────┘
#
# 视觉细节 (阈值线 / 颜色 / 字体) 不要改 — 前端 MJPEG 习惯了的视觉对照会崩.
# =============================================================================
from __future__ import annotations

from typing import Any, Iterable, Optional

import cv2
import numpy as np


# ===========================================================================
#                                 常量
# ===========================================================================

# Scale Sobel-x viz 的二值化阈值
SCALE_BIN_THR_LOW: int = 30
SCALE_BIN_THR_HIGH: int = 60

# tube_edges_binary 总宽
TUBE_EDGES_VIZ_W: int = 640

# scale_sobel_x_binary 单格尺寸
SCALE_PANE_W: int = 320
SCALE_PANE_H: int = 400

# liquid_signal_panel 总宽 + plot 高
LIQ_PANEL_W: int = 640
LIQ_PANEL_PLOT_H: int = 200


# ===========================================================================
#                       1. tube_edges_binary
# ===========================================================================

def make_tube_edges_viz(bgr: np.ndarray, st: Any) -> Optional[np.ndarray]:
    """Tube edges viz (Pipeline binarization map: #1 #2 #3).

    抽自 detector.py:_make_tube_edges_viz (L272-326). 上下双格 W=640:
      上格: bgr 等比缩到 W=640, 裁 [y0..y1] 段 → 灰度化 + 蓝色 #3 mask 多边形轮廓 (定位锚)
      下格: 纯 #1 edges_otsu 黑底白线 (INTER_NEAREST 缩放保持 0/255)
    """
    if st is None or not getattr(st, 'ok', False):
        return None
    if getattr(st, 'y0', None) is None or getattr(st, 'y1', None) is None:
        return None
    try:
        h, w = bgr.shape[:2]
        y0, y1 = int(st.y0), int(st.y1)
        if y1 - y0 < 10:
            return None
        W_OUT = TUBE_EDGES_VIZ_W
        H_full = int(round(W_OUT * h / max(1, w)))
        bgr_resized = cv2.resize(bgr, (W_OUT, H_full), interpolation=cv2.INTER_AREA)
        y0_b = max(0, int(round(y0 / max(1, h - 1) * (H_full - 1))))
        y1_b = min(H_full - 1, int(round(y1 / max(1, h - 1) * (H_full - 1))))
        if y1_b <= y0_b + 5:
            return None
        bgr_strip = bgr_resized[y0_b:y1_b + 1, :].copy()
        H_strip = bgr_strip.shape[0]

        # 上格: gray + #3 mask 多边形蓝色轮廓
        gray_strip = cv2.cvtColor(bgr_strip, cv2.COLOR_BGR2GRAY)
        upper = cv2.cvtColor(gray_strip, cv2.COLOR_GRAY2BGR)
        if getattr(st, 'mask', None) is not None:
            try:
                mask_resized = cv2.resize(st.mask, (W_OUT, H_full), interpolation=cv2.INTER_NEAREST)
                mask_strip = mask_resized[y0_b:y1_b + 1, :]
                contours, _ = cv2.findContours(mask_strip, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                cv2.drawContours(upper, contours, -1, (255, 0, 0), 2)
            except Exception:
                pass
        cv2.rectangle(upper, (0, 0), (W_OUT, 24), (0, 0, 0), -1)
        cv2.putText(upper, "[A] tube ROI:  gray + #3 mask outline (location anchor)",
                    (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 220, 40), 1, cv2.LINE_AA)

        # 下格: 纯 #1 edges_otsu 二值
        edges_otsu = getattr(st, 'edges_otsu', None)
        if edges_otsu is not None:
            edges_resized = cv2.resize(edges_otsu, (W_OUT, H_strip), interpolation=cv2.INTER_NEAREST)
            lower = cv2.cvtColor(edges_resized, cv2.COLOR_GRAY2BGR)
        else:
            lower = np.zeros((H_strip, W_OUT, 3), np.uint8)
        cv2.rectangle(lower, (0, 0), (W_OUT, 24), (0, 0, 0), -1)
        cv2.putText(lower, "#1 edges_otsu:  OTSU(|Sobel-y|) binary (white=edge)",
                    (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        return cv2.vconcat([upper, lower])
    except Exception:
        return None


# ===========================================================================
#                       2. scale_sobel_x_binary
# ===========================================================================

def make_scale_sobel_x_viz(
    bgr: np.ndarray,
    st: Any,
    res: dict,
    *,
    band_x: int = 150,
) -> Optional[np.ndarray]:
    """Scale Sobel-x 4-pane viz (Pipeline binarization map: #4 #5).

    抽自 detector.py:_make_scale_sobel_x_viz (L334-421). 2x2 布局:
      ┌──────────────┬──────────────┐
      │ gray         │ |Sobel-x|    │
      ├──────────────┼──────────────┤
      │ thr=LOW (#5) │ thr=HIGH (#5)│
      └──────────────┴──────────────┘
    每格 SCALE_PANE_W × SCALE_PANE_H, 红虚线 = best_x. 阈值来自模块常量 SCALE_BIN_THR_LOW/HIGH.
    """
    if res is None or st is None or not getattr(st, 'ok', False):
        return None
    gx = res.get('gx')
    if gx is None or getattr(st, 'y0', None) is None or getattr(st, 'y1', None) is None:
        return None
    try:
        h, w = bgr.shape[:2]
        cx = int(res.get('cx', w // 2))
        y0, y1 = int(st.y0), int(st.y1)
        cx_lo = max(0, cx - int(band_x))
        cx_hi = min(w, cx + int(band_x))
        if cx_hi - cx_lo < 10 or y1 - y0 < 10:
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        g_roi = gray[y0:y1 + 1, cx_lo:cx_hi]
        gx_roi = gx[y0:y1 + 1, cx_lo:cx_hi]
        thr_low = SCALE_BIN_THR_LOW
        thr_high = SCALE_BIN_THR_HIGH
        bin_lo = (gx_roi > thr_low).astype(np.uint8) * 255
        bin_hi = (gx_roi > thr_high).astype(np.uint8) * 255

        pane_size = (SCALE_PANE_W, SCALE_PANE_H)
        roi_w = cx_hi - cx_lo
        best_x = res.get('x')
        bx_local_pane = None
        if best_x is not None:
            bx_orig = int(best_x) - cx_lo
            if 0 <= bx_orig < roi_w:
                bx_local_pane = int(round(bx_orig / max(1, roi_w - 1) * (pane_size[0] - 1)))

        def _to_pane(arr2d, kind):
            resized = cv2.resize(arr2d, pane_size, interpolation=cv2.INTER_AREA)
            if kind == 'red_binary':
                pane = np.zeros((pane_size[1], pane_size[0], 3), np.uint8)
                pane[resized > 0] = (0, 0, 255)
            else:
                pane = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGR)
            return pane

        p_gray = _to_pane(g_roi, 'gray')
        p_gx = _to_pane(gx_roi, 'gray')
        p_lo = _to_pane(bin_lo, 'red_binary')
        p_hi = _to_pane(bin_hi, 'red_binary')

        for pane, label in (
            (p_gray, "gray"),
            (p_gx, "|Sobel-x|"),
            (p_lo, f"thr={thr_low}  (SCALE_BIN_THR_LOW)"),
            (p_hi, f"thr={thr_high}  (SCALE_BIN_THR_HIGH)"),
        ):
            ph, pw = pane.shape[:2]
            if bx_local_pane is not None and 0 <= bx_local_pane < pw:
                for ys in range(0, ph, 8):
                    cv2.line(pane, (bx_local_pane, ys),
                             (bx_local_pane, min(ph - 1, ys + 4)), (0, 0, 255), 2)
            cv2.rectangle(pane, (0, 0), (260, 26), (0, 0, 0), -1)
            cv2.putText(pane, label, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 2, cv2.LINE_AA)

        top = cv2.hconcat([p_gray, p_gx])
        bot = cv2.hconcat([p_lo, p_hi])
        out = cv2.vconcat([top, bot])
        H_out, W_out = out.shape[:2]
        info = (f"cx={cx} best_x={best_x} score={res.get('score', 0):.0f} "
                f"conf={res.get('conf', 0):.2f} (conf_min=0.10)  "
                f"|  red dashed = best_x picked by find_center_scale_line")
        cv2.rectangle(out, (0, H_out - 26), (W_out, H_out - 1), (0, 0, 0), -1)
        cv2.putText(out, info, (8, H_out - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (40, 220, 40), 1, cv2.LINE_AA)
        return out
    except Exception:
        return None


# ===========================================================================
#                       3. liquid_signal_panel
# ===========================================================================

def make_liquid_signal_panel(
    bgr: np.ndarray,
    signals: dict,
    *,
    peak_strength: Optional[float] = None,
    decision: Optional[str] = None,
    y0: Optional[int] = None,
    y1: Optional[int] = None,
    raw_history: Optional[Iterable[int]] = None,
    last_raw: Optional[int] = None,
) -> Optional[np.ndarray]:
    """Liquid 1D signal panel ([E2] sobel_proj 链, Pipeline binarization map: #7-#10).

    抽自 detector.py:_make_liquid_signal_panel (L427-602). [E] 液面定容主路径上**没有 2D
    binary mask** —— 全程是 1D Sobel 信号链. "二值化" 语义在这里 = 曲线是否过阈值横线.

    上半 banner (640×~256): bgr 灰度化等比缩到 W=640 + 裁 [y0..y1] 段.
      - 蓝色半透明框: detect ROI [x1, x2]
      - 累积红云带 (raw_history): 反映液面 raw_max_x 的漂移历史
      - 强红粗线: 当前帧 last_raw
      - 黄色竖线: peaks[1:]
    下半 plot 640 × 200: 4 条曲线 (base/proj/diff/smooth) + 3 阈值线 + peaks ★.
    """
    if signals is None:
        return None
    try:
        smooth = signals.get('edge_smooth')
        if smooth is None or smooth.size < 2:
            return None
        base = signals.get('sobel_proj_base')
        proj = signals.get('sobel_proj')
        diff = signals.get('edge_diff')
        peaks = signals.get('peaks', []) or []
        x1 = int(signals.get('x1', 0))
        x2 = int(signals.get('x2', 0))

        h_img, w_img = bgr.shape[:2]
        W = LIQ_PANEL_W
        H_full = int(round(W * h_img / max(1, w_img)))
        banner_full_bgr = cv2.resize(bgr, (W, H_full), interpolation=cv2.INTER_AREA)
        banner_full_gray = cv2.cvtColor(banner_full_bgr, cv2.COLOR_BGR2GRAY)
        banner_full = cv2.cvtColor(banner_full_gray, cv2.COLOR_GRAY2BGR)
        if y0 is not None and y1 is not None and int(y1) > int(y0):
            y0_b = max(0, int(round(int(y0) / max(1, h_img - 1) * (H_full - 1))))
            y1_b = min(H_full - 1, int(round(int(y1) / max(1, h_img - 1) * (H_full - 1))))
            if y1_b > y0_b + 5:
                banner = banner_full[y0_b:y1_b + 1, :].copy()
            else:
                banner = banner_full
        else:
            banner = banner_full
        H_BANNER = banner.shape[0]

        def _abs_to_bx(absx):
            return int(round(int(absx) / max(1, w_img - 1) * (W - 1)))

        # 累积红云带
        if raw_history:
            weights = np.zeros(W, dtype=np.float32)
            for absx in raw_history:
                if absx is None:
                    continue
                bx_w = _abs_to_bx(absx)
                if 0 <= bx_w < W:
                    weights[bx_w] += 1.0
            if weights.max() > 0:
                ks = 9
                kernel = cv2.getGaussianKernel(ks, 0)
                weights = cv2.filter2D(weights.reshape(-1, 1), -1, kernel).ravel()
                red_add = (weights * 255.0 / 5.0).clip(0, 255).astype(np.uint8)
                red_add[red_add < 50] = 0
                red_add_2d = np.broadcast_to(red_add, (H_BANNER, W))
                banner[..., 2] = np.maximum(banner[..., 2], red_add_2d)

        # 蓝色半透明 ROI 框
        bx1 = _abs_to_bx(x1)
        bx2 = _abs_to_bx(x2)
        if bx2 > bx1:
            overlay = banner.copy()
            cv2.rectangle(overlay, (bx1, 0), (bx2, H_BANNER - 1), (255, 100, 0), -1)
            banner = cv2.addWeighted(overlay, 0.22, banner, 0.78, 0)
            cv2.rectangle(banner, (bx1, 0), (bx2, H_BANNER - 1), (255, 140, 30), 1)

        # 当前帧其它 peaks 黄色细线
        if peaks and len(peaks) > 1:
            for c in peaks[1:]:
                absx = x1 + int(c)
                if absx < 0 or absx >= w_img:
                    continue
                bx = _abs_to_bx(absx)
                cv2.line(banner, (bx, 0), (bx, H_BANNER - 1), (0, 220, 255), 1)

        # 当前帧 leftmost peak (last_raw) 强红粗线
        if last_raw is not None and 0 <= int(last_raw) < w_img:
            bx_lr = _abs_to_bx(last_raw)
            cv2.line(banner, (bx_lr, 0), (bx_lr, H_BANNER - 1), (0, 0, 255), 2)

        # 顶/底 文字说明
        cv2.rectangle(banner, (0, 0), (W, 22), (0, 0, 0), -1)
        cv2.putText(
            banner,
            "[E2] LIQUID 1D SIGNAL  (NO 2D binary on this branch)",
            (6, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 220, 255), 1, cv2.LINE_AA,
        )
        cv2.rectangle(banner, (0, H_BANNER - 22), (W, H_BANNER - 1), (0, 0, 0), -1)
        cv2.putText(
            banner,
            "blue=ROI[x1,x2]  red=raw_max_x (history alpha; bold=current)  yellow=other peaks",
            (6, H_BANNER - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA,
        )

        H_PLOT = LIQ_PANEL_PLOT_H
        plot = np.full((H_PLOT, W, 3), 30, np.uint8)
        n = int(smooth.size)
        arrs = [a for a in (base, proj, diff, smooth) if a is not None and getattr(a, 'size', 0) > 0]
        vmax = max((float(np.max(a)) for a in arrs), default=1.0)
        vmax = max(vmax, 100.0)
        pad_top, pad_bot = 26, 22
        plot_h = H_PLOT - pad_top - pad_bot

        def _ys(arr):
            a = np.clip(np.asarray(arr, np.float32), 0, vmax)
            return (pad_top + plot_h - 1 - (a / vmax * (plot_h - 1))).astype(np.int32)

        for thr, color, label in (
            (50, (0, 220, 220), "h=50 (#9 height_thr)"),
            (30, (220, 220, 0), "p=30 (#9 prom)"),
            (80, (0, 0, 255), "S=80 (#10 STRENGTH)"),
        ):
            if thr <= vmax:
                yy = pad_top + plot_h - 1 - int(thr / vmax * (plot_h - 1))
                for xs in range(0, W, 12):
                    cv2.line(plot, (xs, yy), (min(W - 1, xs + 6), yy), color, 1)
                cv2.putText(plot, label, (W - 200, max(12, yy - 2)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)

        legend_x = 10
        for arr, color, lab, thick in (
            (base,   (180, 180, 180), "base",   1),
            (proj,   (60, 200, 60),   "proj",   1),
            (diff,   (60, 140, 240),  "diff",   1),
            (smooth, (240, 180, 60),  "smooth", 2),
        ):
            if arr is None or getattr(arr, 'size', 0) < 2:
                continue
            xs = np.linspace(0, W - 1, arr.size).astype(np.int32)
            ys = _ys(arr)
            pts = np.stack([xs, ys], axis=1)
            cv2.polylines(plot, [pts], False, color, thick)
            cv2.putText(plot, lab, (legend_x, 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
            legend_x += 70

        if peaks:
            for i, c in enumerate(peaks):
                if c < 0 or c >= n:
                    continue
                xx = int(round(c / max(1, n - 1) * (W - 1)))
                val = float(smooth[c])
                yy = pad_top + plot_h - 1 - int(np.clip(val, 0, vmax) / vmax * (plot_h - 1))
                color = (0, 0, 255) if i == 0 else (0, 220, 255)
                cv2.drawMarker(plot, (xx, yy), color, cv2.MARKER_STAR, 14, 2)

        ps_txt = f"{peak_strength:.1f}" if isinstance(peak_strength, (int, float)) else "?"
        info = f"peak_strength={ps_txt} decision={decision}"
        cv2.putText(plot, info, (10, H_PLOT - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1, cv2.LINE_AA)

        return cv2.vconcat([banner, plot])
    except Exception:
        return None


# ===========================================================================
#                       4. vial_overlay (locked-mode 三线叠加)
# ===========================================================================

def make_vial_overlay_locked(
    base_viz: Optional[np.ndarray],
    fallback_bgr: np.ndarray,
    *,
    scale_line_x: int,
    locked_y0: Optional[int],
    locked_y1: Optional[int],
    last_raw: Optional[int] = None,
    l2s: Optional[int] = None,
    label: Optional[str] = None,
) -> np.ndarray:
    """Locked-mode vial_overlay 三线叠加 (抽自 detector.py:_process_locked_mode L1452-1485).

    在 base_viz (= st.viz, 含 tube mask + y0/y1 + xl/xr) 上叠加:
      - 黄实线粗 : scale_line_x (锁定的刻度位置)
      - 淡绿细  : last_raw (li_state['last_raw'], 算法当前帧 raw_max_x)
      - 亮绿粗  : scale_line_x + l2s (当前发布的 l2s 对应位置)
      - 顶部 label (可选)

    base_viz=None 时 fallback 到 fallback_bgr (= 当前帧). l2s=None / 10000 时不画亮绿线.
    """
    vis = base_viz.copy() if base_viz is not None else fallback_bgr.copy()
    try:
        H = vis.shape[0]
        y0_src = locked_y0 if locked_y0 is not None else 0
        y1_src = locked_y1 if locked_y1 is not None else (H - 1)
        y0_v = max(0, min(H - 1, int(y0_src) + 3))
        y1_v = max(0, min(H - 1, int(y1_src) - 3))

        # 黄实线: 锁定刻度 x
        cv2.line(vis, (int(scale_line_x), y0_v), (int(scale_line_x), y1_v), (0, 255, 255), 2)
        # 淡绿细: raw 液面位
        if last_raw is not None:
            cv2.line(vis, (int(last_raw), y0_v), (int(last_raw), y1_v), (120, 220, 120), 1)
        # 亮绿粗: 发布的 l2s 对应位置 (l2s == 10000 视为无效占位, 不画)
        if l2s is not None and int(l2s) != 10000:
            xl = int(scale_line_x) + int(l2s)
            cv2.line(vis, (xl, y0_v), (xl, y1_v), (0, 200, 0), 2)
        # label
        if label:
            cv2.putText(vis, label, (8, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (40, 200, 40), 2, cv2.LINE_AA)
    except Exception:
        pass
    return vis
