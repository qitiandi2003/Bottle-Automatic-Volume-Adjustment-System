#=============================================================================
# li_det.py —— 鎖定模式液面檢測 (lock-mode 主循環 helper)
# -----------------------------------------------------------------------------
# 對外 4 個入口:
#   reset_liquid_state               —— 建初始 tracker state dict
#   calibrate_locked_frame           —— 鎖定瞬間建 sobel_proj_base / gray_col_means baseline
#   update_liquid_level              —— 每幀: detect → median → monotonic clamp → pin
#   detect_liquid_level_by_histogram —— update_liquid_level 內部 helper, 也供 viz panel 拉信號
# =============================================================================
import cv2
import numpy as np
from collections import deque

LIQ_PEAK_MIN_STRENGTH = 60      # _find_edge_peaks 最左 peak 強度 < 此值 → 視為噪聲, 拒絕採信
LIQ_REGRESSION_DELTA = 40      # long-median 比 pin 大 > 此值 → 觸發回落 (pin 是假鎖)
LIQ_REGRESSION_FRAMES = 5       # 連續 K 幀 long-median 高於 pin + DELTA 才回落
LIQ_INIT_FRAMES = 2             # init 階段需要 K 幀; 同時用作 forward 階段的 median 平滑窗口
LIQ_RAW_HISTORY_LEN = 20        # raw deque 長度
TERMINAL_LOCK_DELTA = 180       # 距離刻度線 < 此值則不觸發回落 (因為 scale 線附近干擾邊緣常常比真液面更強)


def reset_liquid_state():
    """返回新的液面 tracker state dict. detector 解鎖時 / 初始化時調用.
    detector 持引用, li_det 函數讀寫. 避免在 detector 端維護 deques / amp gates."""
    return {
        'pinned_max_x': None,                       # 當前釘位
        'raw_history': deque(maxlen=LIQ_RAW_HISTORY_LEN),
        'last_raw': None,                           # 最後一幀 raw 結果 (給 viz 用)
        'last_peak_strength': 0.0,                  # 最後一幀 peak 強度
        'last_decision': 'init',                    # init/init_wait/forward/regression/hold/no_peak/weak_peak
        'last_peaks_lmr': [],                       # 最後一幀 leftmost-3 peaks [(abs_x, strength), ...]; 診斷干擾邊緣 vs 真液面競爭用
         # 新增
        'ever_forward': False,           # 一旦液面真实向刻度方向推进过，就禁止右回跳
        'regression_count': 0,
    }


def update_liquid_level(img, res, state):
    """鎖定模式每幀調用. 邏輯 (方案 B: 3-frame median + monotonic clamp):
       1. detect_liquid_level_by_histogram → (raw_max_x, peak_strength, peaks_lmr, signals)
       2. peak 弱 → weak_peak, 不入 history
       3. candidate = median(raw_history[-K:])  K=LIQ_INIT_FRAMES, 抗 1 幀 outlier
       4. monotonic clamp: candidate < pinned → forward; 否則 hold (跳幅多大都採信, 不像 jump_reject 那樣攔)
       5. 連續 K 幀 long-median 比 pin 大 > DELTA → regression (pin 假鎖兜底)
    返回 (pinned_max_x, raw_max_x, decision, peak_strength, signals).
    signals: dict from detect_liquid_level_by_histogram for liquid_signal_panel viz; may be None if ROI invalid.
    state 為 reset_liquid_state() 返回的 dict, 函數會就地修改 (含 last_peaks_lmr 給日誌診斷)."""
    raw_max_x, peak_strength, peaks_lmr, signals = detect_liquid_level_by_histogram(img, res)
    state['last_raw'] = raw_max_x
    state['last_peak_strength'] = peak_strength
    state['last_peaks_lmr'] = peaks_lmr

    if raw_max_x is None:
        state['last_decision'] = 'no_peak'
        return state['pinned_max_x'], None, 'no_peak', peak_strength, signals

    state['raw_history'].append(raw_max_x)
    scale_x = int(res['scale_line_x'])

    recent = list(state['raw_history'])[-LIQ_INIT_FRAMES:]
    if len(recent) < LIQ_INIT_FRAMES:
        state['last_decision'] = 'init_wait'
        return state['pinned_max_x'], raw_max_x, 'init_wait', peak_strength, signals
    candidate = int(np.median(recent))

    decision = 'hold'
    if state['pinned_max_x'] is None:
        state['pinned_max_x'] = candidate
        decision = 'init'
    elif candidate < state['pinned_max_x']:
        state['pinned_max_x'] = candidate
        state['ever_forward'] = True
        state['regression_count'] = 0
        decision = 'forward'
    # else:
    #     recent_long = list(state['raw_history'])[-LIQ_REGRESSION_FRAMES:]
    #     if (len(recent_long) >= LIQ_REGRESSION_FRAMES and
    #             int(np.median(recent_long)) > state['pinned_max_x'] + LIQ_REGRESSION_DELTA):
    #         state['pinned_max_x'] = int(np.median(recent_long))
    #         decision = 'regression'
    
    # regression 回退机制：
    # 正常情况下，液面只允许单向向左推进（candidate < pinned_max_x）。
    # 但如果连续多帧(long-median)都明显大于当前 pin，
    # 说明当前 pin 可能被错误锁定在某个噪声峰/假液面上，
    # 此时允许 pin 回退（regression）到新的稳定位置。
    #
    # 额外加入 near_scale 终点保护：
    # 当液面已经接近刻度线时（距离小于 TERMINAL_LOCK_DELTA），
    # 禁止 regression 回退。
    #
    # 原因：
    # 在球腹→细口过渡区，容易产生强阴影边缘；
    # 接近定容终点时，真实液面已经接近刻度，
    # 若阴影重新被识别为液面，会导致 pin 回跳，
    # l2s 重新变大，从而继续滴液并最终溢出。
    #
    # 因此：
    # 接近刻度后进入 terminal-lock 模式，
    # 液面只允许继续前进，不允许任何回退，
    # 用物理单调性约束抑制阴影误检。
    else:
        near_scale = (
            state['pinned_max_x'] - scale_x
            < TERMINAL_LOCK_DELTA
        )
        if not near_scale:
            recent_long = list(
                state['raw_history']
            )[-LIQ_REGRESSION_FRAMES:]
            if (
                len(recent_long)
                >= LIQ_REGRESSION_FRAMES
                and int(np.median(recent_long))
                > state['pinned_max_x']
                + LIQ_REGRESSION_DELTA
            ):
                state['pinned_max_x'] = int(
                    np.median(recent_long)
                )
                decision = 'regression'

    if state['pinned_max_x'] is not None and state['pinned_max_x'] < scale_x:
        state['pinned_max_x'] = scale_x

    state['last_decision'] = decision
    return state['pinned_max_x'], raw_max_x, decision, peak_strength, signals


def calibrate_locked_frame(img, locked_x_scale, locked_y0, locked_y1, locked_detection_start_x):
    """鎖定模式專用首幀標定 (取代 calibrate_first_frame). 全 ROI, 不跑 preprocess_image /
    analyze_calibration / trace_line_8neighbor. scale_line_x 在 unlocked 階段已由
    find_center_scale_line + GapStability 鎖定確認, 本函數只算 sobel_proj_base."""
    import time as _time
    t0 = _time.monotonic()
    h, w = img.shape[:2]

    y1, y2 = sorted([int(locked_y0), int(locked_y1)])
    x1, x2 = sorted([int(locked_x_scale), int(locked_detection_start_x)])
    
    pad = 50
    rx1 = max(0, x1 - pad)
    rx2 = min(w, x2 + pad)
    ry1 = max(0, y1 - pad)
    ry2 = min(h, y2 + pad)
    roi_g = img[ry1:ry2, rx1:rx2, 1]

    sub_y1 = y1 - ry1
    sub_y2 = y2 - ry1
    sub_x1 = x1 - rx1
    sub_x2 = x2 - rx1

    t1 = _time.monotonic()
    roi_g_med = cv2.medianBlur(roi_g, 15)

    t2 = _time.monotonic()
    gray_green_f = roi_g_med.astype(np.float32)
    res = {
        'scale_line_x': int(x1),
        'detection_start_x': int(x2),
        'y_a': int(y1),
        'y_b': int(y2),
        'gray_col_means': np.mean(gray_green_f[sub_y1:sub_y2, sub_x1:sub_x2], axis=0),
        'gray_roi_base': gray_green_f[sub_y1:sub_y2, sub_x1:sub_x2].copy(),
    }

    t3 = _time.monotonic()
    blurred = cv2.GaussianBlur(roi_g, (3, 3), 0).astype(np.float32)
    h_roi = y2 - y1
    margin_y = max(5, h_roi // 5)
    sub_y1c = sub_y1 + margin_y
    sub_y2c = sub_y2 - margin_y
    roi_center = blurred[sub_y1c:sub_y2c, sub_x1:sub_x2]
    sobel_x = cv2.Sobel(roi_center, cv2.CV_32F, 1, 0, ksize=3)
    res['sobel_proj_base'] = np.mean(np.abs(sobel_x), axis=0)
    res['gray_col_means_gauss'] = np.mean(roi_center, axis=0)
    res['center_roi_margin_y'] = margin_y

    t4 = _time.monotonic()
    print(f"[calibrate_locked] ROI={ry2-ry1}x{rx2-rx1} medianBlur={1000*(t2-t1):.1f}ms "
          f"GaussianBlur+Sobel={1000*(t4-t3):.1f}ms total={1000*(t4-t0):.1f}ms "
          f"scale_x={x1} detect_start={x2} y=({y1},{y2})")
    return res




def _find_edge_peaks(arr, height_thr=50, min_distance=30, prominence_thr=30):
    """在1D数组中找局部极大值，返回按位置排序的峰索引列表。

    过滤条件（三选一必须全过）：
      - 绝对高度 >= height_thr
      - 突出度 prominence >= prominence_thr
        prominence 定义：当前峰与两侧"最近的更高峰"之间的最低谷的差值
      - 与已选峰的距离 >= min_distance（太近则只保留更高者）
    """
    peaks = []
    n = len(arr)
    for i in range(1, n - 1):
        if arr[i] < height_thr:
            continue
        # 局部最大值
        if arr[i] <= arr[i - 1] or arr[i] <= arr[i + 1]:
            continue
        # 检查prominence: 到两侧最近更高峰之间的最低谷
        # 向左走：记录过程中的最小值；遇到比 arr[i] 高的就停
        left_min = arr[i]
        for j in range(i - 1, -1, -1):
            left_min = min(left_min, arr[j])
            if arr[j] > arr[i]:
                break
        right_min = arr[i]
        for j in range(i + 1, n):
            right_min = min(right_min, arr[j])
            if arr[j] > arr[i]:
                break
        # 突出度 = 当前值 - max(左最低, 右最低)
        prom = arr[i] - max(left_min, right_min)
        if prom < prominence_thr:
            continue
        # 检查与已有peaks的距离
        if len(peaks) > 0 and (i - peaks[-1]) < min_distance:
            # 保留更高的那个
            if arr[i] > arr[peaks[-1]]:
                peaks[-1] = i
            continue
        peaks.append(i)
    return peaks



def detect_liquid_level_by_histogram(img, res):
    """純檢測 (lock 模式內部 helper, 由 update_liquid_level 調用):
       高斯模糊 + Sobel-x + ROI 垂直投影 + 減基準 + 找最左峰 → 返回 (raw_max_x, peak_strength, peaks_lmr, signals).
    peaks_lmr: 最多前 3 個 peak (按 leftmost 排序), [(abs_x, strength), ...]; 診斷干擾邊緣與真液面競爭用.
    signals: dict with sobel_proj_base / sobel_proj / edge_diff / edge_smooth / peaks (1D arrays + index list) +
             x1, x2 (ROI column range in full-image coords). For liquid_signal_panel viz (TODO.md map: #7-#10).
             None when ROI too small or res missing.
    無 monotonic clamp / 無 reach 判定 / 無 prev_liquid_x — 那些屬於上層 update_liquid_level."""
    if res is None or res.get('sobel_proj_base') is None:
        return None, 0.0, [], None

    y1, y2 = sorted([int(res['y_a']), int(res['y_b'])])
    x1, x2 = sorted([int(res['scale_line_x']), int(res['detection_start_x'])])
    h_roi = y2 - y1
    w_roi = x2 - x1

    if h_roi < 10 or w_roi < 10:
        return None, 0.0, [], None

    # 高斯模糊 G 通道全圖 (主循環優化目標, 見 TODO.md "Locked-mode main-loop optimization")
    blurred = cv2.GaussianBlur(img[:, :, 1], (3, 3), 0).astype(np.float32)
    margin_y = res.get('center_roi_margin_y', max(5, h_roi // 5))
    y1c, y2c = y1 + margin_y, y2 - margin_y
    roi_center = blurred[y1c:y2c, x1:x2]
    n = min(roi_center.shape[1], res['sobel_proj_base'].size)

    sobel_x = cv2.Sobel(roi_center[:, :n], cv2.CV_32F, 1, 0, ksize=3)
    sobel_proj = np.percentile(np.abs(sobel_x), 85, axis=0)
    base_proj = res['sobel_proj_base'][:n]

    edge_diff = np.log1p(sobel_proj) - np.log1p(base_proj)
    edge_diff *= 40

    ks = 10
    if n >= ks:
        edge_smooth = np.convolve(edge_diff, np.ones(ks) / ks, mode='same')
    else:
        edge_smooth = edge_diff.copy()

    peaks = _find_edge_peaks(edge_smooth, height_thr=50, min_distance=32, prominence_thr=32)

    # signals dict for liquid_signal_panel viz (always populated even if no peaks, so panel keeps drawing)
    signals = {
        'sobel_proj_base': base_proj,
        'sobel_proj': sobel_proj,
        'edge_diff': edge_diff,
        'edge_smooth': edge_smooth,
        'peaks': list(peaks),
        'x1': int(x1),
        'x2': int(x1 + n),
    }

    if len(peaks) == 0:
        return None, 0.0, [], signals

    peaks_lmr = [(int(x1 + c), float(edge_smooth[c])) for c in peaks[:3]]
    front_col = peaks[0]
    peak_strength = float(edge_smooth[front_col])
    raw_max_x = int(x1 + front_col)
    return raw_max_x, peak_strength, peaks_lmr, signals