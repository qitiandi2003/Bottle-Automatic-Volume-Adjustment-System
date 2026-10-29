#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot auto exposure adjustment triggered on tube detection transition.

API:
  auto_adjust_exposure_once(cam, bgr, state, tube_type='auto', **kwargs) -> dict

Notes:
  - Uses tube window from `state` (y0/y1/x_left/x_right) to build ROI
  - Computes gray median and simple saturation metrics
  - Chooses a target median (different for transparent/brown) and adjusts ExposureTime
  - Safe clamps and prints concise debug info

==============================================================================
模块作用：
    在"刚识别到试管"的那一刻做一次自动曝光调整：
      1) 根据试管几何 state 截出 ROI；
      2) 在 ROI 里算灰度中位数 med、高光饱和比 sat、95/99 分位；
      3) 按试管类型（透明管 / 棕色管）选择目标亮度；
      4) 计算新的曝光时间 new_exp（理论上 new_exp = prev_exp * target/med），
         带上高光保护、饱和保护、单次步长限制、物理量 clamp；
      5) 写回相机。
    "one-shot" 指的是：每次试管出现只调一次，不做连续闭环曝光控制。
==============================================================================
"""

from __future__ import annotations

import cv2
import numpy as np
from typing import Optional, Dict, Any, Tuple

def _safe_gray(bgr: np.ndarray) -> np.ndarray:
    """健壮地把输入图像转成单通道灰度。

    正常情况直接 BGR→Gray；异常时做两种回退：
        - 已经是 2 维，当成灰度返回；
        - 以 RGB 顺序当 BGR 处理（处理第三方库把 RGB 当 BGR 的情况）。
    """
    try:
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    except Exception:
        if bgr.ndim == 2:
            return bgr
        # [..., ::-1] 反转最后一维通道：B,G,R → R,G,B
        return cv2.cvtColor(bgr[..., ::-1], cv2.COLOR_BGR2GRAY)


def _roi_from_state(bgr: np.ndarray, state, y_margin: int = 3, x_margin: int = 5) -> Optional[Tuple[np.ndarray, Tuple[int, int, int, int]]]:
    """根据几何状态 state 切出 ROI。
    state 预期有属性 y0/y1/x_left/x_right（都是图像坐标）。
    y_margin/x_margin 用来再向内收一点边缘，避开管壁本身的黑边。
    同时若 state.mask 存在，用它 refine x 的范围让 ROI 更贴合试管。
    """
    h, w = bgr.shape[:2]
    try:
        # 注意这里的 margin 是"向内收"：y0 加、y1 减、xl 加、xr 减
        y0 = max(0, min(h - 1, int(state.y0) + y_margin))
        y1 = max(0, min(h - 1, int(state.y1) - y_margin))
        xl = max(0, min(w - 1, int(state.x_left) + x_margin))
        xr = max(0, min(w - 1, int(state.x_right) - x_margin))
        # Use mask columns if available to widen ROI
        # 若有 mask，就依 mask 的"列分布"重新定 x 范围（再向外扩 x_margin）
        try:
            m = getattr(state, 'mask', None)
            if m is not None:
                sub = m[y0:y1 + 1]
                if sub.size:
                    cols = np.where(sub.any(axis=0))[0]
                    if cols.size:
                        xl = max(0, int(cols[0]) - x_margin)
                        xr = min(w - 1, int(cols[-1]) + x_margin)
        except Exception:
            pass
        if y1 <= y0 or xr <= xl:
            return None              # ROI 变成 0/负面积 → 无效
        roi = bgr[y0:y1 + 1, xl:xr + 1].copy()
        return roi, (y0, y1, xl, xr)
    except Exception:
        return None


def _guess_tube_type(bgr_roi: np.ndarray) -> str:
    try:
        gray = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2GRAY)

        h, w = gray.shape

        # 只取中心20%
        x1 = int(w * 0.40)
        x2 = int(w * 0.60)

        center = gray[:, x1:x2]

        profile = np.mean(center, axis=1).astype(np.float32)

        mean_v = float(np.mean(profile))
        std_v = float(np.std(profile))

        print(
            f"[tube-class] "
            f"mean={mean_v:.1f} "
            f"std={std_v:.1f}"
        )

        score = 0

        # 棕色瓶透光率低
        if mean_v < 220:
            score += 2

        # 棕色瓶亮度变化更明显
        if std_v > 20:
            score += 1

        return "brown" if score >= 2 else "transparent"

    except Exception:
        return "transparent"


def _get_exposure_us(cam) -> Optional[float]:
    """Robustly read ExposureTime (us) from different wrapper return styles.

    海康/MV SDK 的 Python 包装在不同版本里，Get 调用返回格式有：
      A) (ret, payload) 元组
      B) 带 fCurValue 字段的 dict
      C) 带 fCurValue 属性的对象
      D) 直接是浮点/整数
    这里依次尝试，拿到微秒（us）值为止。再不行就回退读 ExposureTimeAbs。
    """
    try:
        v = cam.MV_CC_GetFloatValue("ExposureTime")
        # (ret, payload)
        if isinstance(v, tuple) and len(v) >= 2:
            ret, payload = v[0], v[1]
            if ret == 0:
                if isinstance(payload, dict) and 'fCurValue' in payload:
                    return float(payload['fCurValue'])
                if hasattr(payload, 'fCurValue'):
                    return float(getattr(payload, 'fCurValue'))
                try:
                    return float(payload)
                except Exception:
                    pass
        # dict payload
        if isinstance(v, dict) and 'fCurValue' in v:
            return float(v['fCurValue'])
        # object-like
        if hasattr(v, 'fCurValue'):
            return float(getattr(v, 'fCurValue'))
        # direct float/int
        return float(v)
    except Exception:
        # try alternative node name —— 老版 SDK 用 ExposureTimeAbs
        try:
            v = cam.MV_CC_GetFloatValue("ExposureTimeAbs")
            if isinstance(v, dict) and 'fCurValue' in v:
                return float(v['fCurValue'])
            if hasattr(v, 'fCurValue'):
                return float(getattr(v, 'fCurValue'))
            return float(v)
        except Exception:
            return None


def _set_manual_exposure(cam, exp_us: float) -> bool:
    """把相机切到手动曝光并写入新的曝光时间（us）。
    返回 True 表示写入成功，False 表示失败。
    """
    ok = True
    try:
        # ExposureAuto=0 即 Off，关闭自动曝光，否则手动值会被 AE 循环覆盖
        cam.MV_CC_SetEnumValue("ExposureAuto", 0)
    except Exception:
        pass                          # 有些型号无此节点，忽略
    try:
        r = cam.MV_CC_SetFloatValue("ExposureTime", float(exp_us))
        # 返回非 0 的 int 即"失败代码"
        if isinstance(r, int) and r != 0:
            ok = False
    except Exception:
        ok = False
    return ok


def auto_adjust_exposure_once(
    cam,
    bgr: np.ndarray,
    state,
    *,
    tube_type: str = 'auto',                # 'auto' / 'transparent' / 'brown'
    target_median_trans: float = 170.0,     # 透明管目标中位灰度
    target_median_brown: float = 230.0,     # 棕色管目标中位灰度（需要更亮才看得清）
    target_median_override: Optional[float] = None,   # 显式指定目标值则覆盖以上默认
    exp_min_us: float = 3000.0,             # 曝光下限：3 ms
    exp_max_us: float = 40000.0,            # 曝光上限：40 ms（超过会影响帧率）
    sat_high_thr: float = 0.02,             # "中度过曝"阈值：>=250 的像素占比 > 2%
    # Overexposure control
    hi_clip_val: float = 255.0,           # allow slightly higher highlights before clamping
    step_up_max: float = 1.15,             # per-call max multiplier when brightening
    step_down_max: float = 0.85,           # per-call min multiplier when darkening (<=1). 0.6 => allow up to 40% drop
    first_up_abs_max: float = 30000.0,    # cap for first-time (unknown prev) upward target
    first_base_us: float = 12000.0,       # conservative base when prev exposure is unknown
    verbose: bool = True,
    gray_full: Optional[np.ndarray] = None,  # POST-MERGE 共享 trunk gray (full frame); None=旧 _safe_gray(roi2)
) -> Dict[str, Any]:
    """
    Compute a one-shot new ExposureTime and apply.
    Returns dict {ok, prev_exp, new_exp, median, sat_ratio, tube_type}

    输出字典字段：
        ok         : 是否成功写入相机
        prev_exp   : 原曝光 us（可能 None）
        new_exp    : 新曝光 us
        median     : ROI 灰度中位数
        sat_ratio  : >=250 的像素占比（估计过曝程度）
        tube_type  : 最终判定的管型
        log        : 调试日志字符串列表
    """
    out: Dict[str, Any] = {
        'ok': False,
        'prev_exp': None,
        'new_exp': None,
        'median': None,
        'sat_ratio': None,
        'tube_type': tube_type,
        'log': [],
    }
    def _log(msg: str) -> None:
        """把日志追加进 out['log']，并在 verbose 时打印。"""
        try:
            out['log'].append(str(msg))
            if verbose:
                print(f"[auto-expo] {msg}")
        except Exception:
            pass
    roi_info = _roi_from_state(bgr, state, y_margin=3, x_margin=5)
    if roi_info is None:
        _log("roi invalid; skip")
        return out
    roi, (y0, y1, xl, xr) = roi_info
    _log(f"roi size={roi.shape[0]}x{roi.shape[1]}")

    # Minimal tightening: if tube mask is available, crop ROI vertically
    # to the mask's bounding rows (with a small pad). Detection stays unchanged.
    # 进一步 y 方向裁剪：只保留 mask 里有像素的行（上下各留 pad_y 像素缓冲）
    try:
        roi2 = roi
        roi2_mask = None
        roi2_y0_global = int(y0)       # roi2 在全图的 y 起点（后面映射行排除用）
        pad_y = 4
        if hasattr(state, 'mask') and state.mask is not None:
            m = state.mask[y0:y1 + 1, xl:xr + 1]
            # 断言大小一致：以防 mask 形状错配
            if m is not None and m.size == roi.shape[0] * roi.shape[1]:
                rows = np.any(m > 0, axis=1)        # 哪些行在 mask 内有像素
                if np.any(rows):
                    iy = np.where(rows)[0]
                    y0b = max(0, int(iy[0]) - pad_y)
                    y1b = min(roi.shape[0] - 1, int(iy[-1]) + pad_y)
                    if y1b > y0b:
                        roi2 = roi[y0b:y1b + 1, :]
                        roi2_y0_global = int(y0) + int(y0b)
                        try:
                            # roi2_mask 是布尔矩阵，供后续只在有效像素上统计
                            roi2_mask = (m[y0b:y1b + 1, :] > 0)
                        except Exception:
                            roi2_mask = None
                        _log(f"roi y-crop -> {roi2.shape[0]}x{roi2.shape[1]}")
                # if not cropped, still allow using mask within ROI
                if roi2_mask is None:
                    try:
                        roi2_mask = (m > 0)
                    except Exception:
                        roi2_mask = None
    except Exception:
        roi2 = roi
        roi2_mask = None
        roi2_y0_global = int(y0)

    if gray_full is not None:
        # POST-MERGE [B]: gray_full slice 等价于 _safe_gray(roi2) — roi2 是 bgr 的 y/x 切片,
        # 同样 y/x 切片 gray_full 拿到的 gray 与 BGR2GRAY(roi2) 是同一份数据 (因为 BGR2GRAY 是逐像素).
        rh, rw = roi2.shape[:2]
        gray = gray_full[int(roi2_y0_global):int(roi2_y0_global) + rh, int(xl):int(xl) + rw]
    else:
        gray = _safe_gray(roi2)
    # ---------- 使用 tube mask 限定统计范围 ----------
    use_mask = None
    if roi2_mask is not None:
        try:
            if roi2_mask.shape == gray.shape:
                use_mask = (roi2_mask > 0)
        except Exception:
            use_mask = None
    # ---------- 中位数统计（有 mask 则只在 mask 内算）----------
    try:
        if use_mask is not None:
            if np.any(use_mask):
                med = float(np.median(gray[use_mask]))
            else:
                med = None                            # mask 为空 → 无法统计
        else:
            med = float(np.median(gray))
    except Exception:
        med = None
    out['median'] = med
    if med is not None:
        _log(f"median={med:.1f}")

    # ---------- 过曝比例：>=250 的像素在有效集中占比 ----------
    try:
        if use_mask is not None:
            if np.any(use_mask):
                # (gray >= 250) 和 use_mask 都是布尔；mean 得到 [0,1] 的占比
                sat_ratio = float(np.mean((gray >= 250)[use_mask]))
            else:
                sat_ratio = None
        else:
            sat_ratio = float(np.mean(gray >= 250))
    except Exception:
        sat_ratio = None
    out['sat_ratio'] = sat_ratio
    if sat_ratio is not None:
        _log(f"sat={sat_ratio:.3f}")

    # Highlights guard via percentiles
    # ---------- 高光保护：用 p95/p99 分位判断"接近 255 的像素"多不多 ----------
    p95 = None; p99 = None
    try:
        if use_mask is not None:
            if np.any(use_mask):
                vals = gray[use_mask].astype(np.float32)
            else:
                vals = None
        else:
            vals = gray.astype(np.float32)
        if vals is not None and vals.size:
            p95 = float(np.quantile(vals, 0.95))
            p99 = float(np.quantile(vals, 0.99))
            _log(f"p95={p95:.1f} p99={p99:.1f}")
    except Exception:
        pass

    # Tube type decision
    # 若未指定，根据 roi BGR 色彩特征自动判断
    if tube_type == 'auto':
        tube_type = _guess_tube_type(roi)
    out['tube_type'] = tube_type
    # 目标中位灰度：优先用 override，否则按管型挑
    if target_median_override is not None:
        target = target_median_override
    else:
        target = target_median_brown if tube_type == 'brown' else target_median_trans
    _log(f"tube_type={tube_type} target={target:.1f}")

    prev_exp = _get_exposure_us(cam)
    out['prev_exp'] = prev_exp
    if med is None or med <= 1.0:
        # 没统计到亮度 / 几乎全黑 → 放弃调整
        _log("invalid median; skip")
        return out

    # Compute desired scale; bound by highlight safety when brightening
    # 理论曝光比例：要把 med 提到 target，就乘 target/med
    # 公式：  scale = target / med
    scale = float(target / med)
    if p99 is not None and scale > 1.0:
        # prevent pushing 99th percentile above hi_clip_val
        # 曝光放大时，99 分位像素会按比例升高；限制不要超过 hi_clip_val * 0.95
        # safe_scale * p99 ≈ hi_clip_val * 0.95   →   safe_scale = 0.95 * hi_clip_val / p99
        safe_scale = float(hi_clip_val / max(1.0, p99)) * 0.95
        if safe_scale < scale:
            _log(f"highlight guard: scale {scale:.2f} -> {safe_scale:.2f} (p99={p99:.1f})")
            scale = safe_scale

    # Try to get current exposure; if missing, use conservative base
    if prev_exp is None:
        # 拿不到原曝光：用一个保守的 base（钳到 [min,max]）再乘 scale
        base = max(exp_min_us, min(exp_max_us, float(first_base_us)))
        new_prop = base * scale
        _log(f"prev=? base={base:.0f}us scale={scale:.2f} -> {new_prop:.0f}us")
    else:
        # 正常：直接按比例变换
        new_prop = prev_exp * scale
        _log(f"prev={prev_exp:.0f}us scale={scale:.2f} -> {new_prop:.0f}us")

    # Overexposure protection (robust when prev is missing)
    # ---------- 二次过曝保护：根据 sat_ratio 强制降曝光 ----------
    new_exp = new_prop
    prev_base = prev_exp if prev_exp is not None else (max(exp_min_us, min(exp_max_us, float(first_base_us))))
    if sat_ratio is not None:
        guard_factor = None
        if sat_ratio > 0.90:
            guard_factor = 0.20
        elif sat_ratio > sat_high_thr:
            guard_factor = 0.95 if tube_type == 'brown' else 0.52

        if guard_factor is not None:
            guarded = float(prev_base) * guard_factor
            _log(f"sat={sat_ratio:.3f} guard_factor={guard_factor:.2f} -> {guarded:.0f}us")
            new_exp = min(new_prop, guarded)

    # Step limit relative change to avoid abrupt jumps
    # ---------- 步长限幅：避免单次变化过猛 ----------
    if prev_exp is not None:
        if new_exp > prev_exp:
            # 上调最多 step_up_max 倍（如 1.8× ）
            max_up = float(prev_exp) * float(step_up_max)
            if new_exp > max_up:
                _log(f"step limit up: {new_exp:.0f} -> {max_up:.0f}")
            new_exp = min(new_exp, max_up)
        else:
            # 下调最多降到 step_down_max 倍（如 0.6× 即最多降 40%）
            min_down = float(prev_exp) * float(step_down_max)
            if new_exp < min_down:
                _log(f"step limit down: {new_exp:.0f} -> {min_down:.0f}")
            new_exp = max(new_exp, min_down)
    else:
        # First-time upward cap —— 首次无历史值时，封顶 first_up_abs_max
        if new_exp > first_up_abs_max:
            _log(f"first-up cap: {new_exp:.0f} -> {first_up_abs_max:.0f}")
        new_exp = min(new_exp, float(first_up_abs_max))

    # Clamp only (softening disabled)
    # ---------- 硬 clamp 到物理范围 ----------
    before_clamp = new_exp
    new_exp = max(exp_min_us, min(exp_max_us, new_exp))
    if abs(new_exp - before_clamp) > 1e-3:
        _log(f"clamp to [{exp_min_us:.0f},{exp_max_us:.0f}] -> {new_exp:.0f}us")
    _log("soften: disabled")          # "softening" = 平滑过渡，此处已禁用

    # ---------- 写回相机 ----------
    ok = _set_manual_exposure(cam, new_exp)
    out['new_exp'] = new_exp
    out['ok'] = bool(ok)
    _log(f"apply ok={ok}")
    return out
