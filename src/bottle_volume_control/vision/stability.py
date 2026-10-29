#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gap stability monitor over a sliding time window.
Uses time.monotonic() timestamps; stores only valid gap samples (caller ensures filtering).

================================================================================
模块作用：
    在"自动定容"流程里，我们必须能稳定识别到试管上那两条关键刻度线之间的间距
    （gap，单位像素）。实测间距会逐帧小幅抖动（抖动来源：相机噪声、细微振动、
    边缘检测器命中不同的子像素）。本文件用一个"滑动时间窗 + 跨度阈值"的经典方法
    判断："gap 是否在最近 window_s 秒内趋于稳定？" 只有稳定后才允许进入液面检测。

设计思路：
    - 用一条双端队列保存 (时间戳, gap像素值) 的样本流；
    - 每次 update() 都把超过窗口时长的旧样本弹出左端；
    - stats() 按当前窗口内样本算 (min, max, span=max-min, 样本数, 时长)；
    - is_stable() 再套三个条件（样本量 / 跨度 / 时长）一起判定。
================================================================================
"""

from __future__ import annotations           # 让类型注解作为字符串，不参与运行期求值

from collections import deque                # 双端队列：头尾 O(1) 操作，适合滑动窗口
from typing import Deque, Tuple, Optional    # 类型注解，仅用于可读性/IDE 提示


class GapStability:
    """
    间隙稳定性监控器：在滑动时间窗口内收集"间隙值（gap，单位像素）"样本，
    判断在足够长时间、足够多样本下，间隙波动是否小于给定阈值，用于决定是否可锁定刻度并开启液面检测。
    使用 time.monotonic() 时间戳；仅存储调用方已过滤后的有效间隙样本。
    """

    def __init__(self, window_s: float = 20.0, span_thr_px: int = 3, min_samples: int = 30) -> None:
        """
        :param window_s: 滑动窗口时长（秒），只保留窗口内的样本。
                         默认 20 秒 —— 20 s 足够覆盖一次装台/摆放抖动过程
        :param span_thr_px: 间隙跨度阈值（像素），窗口内 (gmax - gmin) 小于此值才认为稳定。
                            默认 3 px —— 实测相机噪声级别
        :param min_samples: 窗口内至少需要的样本数，未达到则不算稳定。
                            默认 30 样本 —— 在 ~10 FPS 处理下约 3 秒数据量
        """
        self.window_s = float(window_s)
        self.span_thr_px = int(span_thr_px)
        self.min_samples = int(min_samples)
        # 双端队列，每项为 (时间戳, 间隙像素值)
        # 使用 deque 是因为 popleft() O(1)；list.pop(0) 要 O(n)
        self.buf: Deque[Tuple[float, int]] = deque()

    def reset(self) -> None:
        """清空所有样本，重新开始统计。
        使用场景：上一根瓶子处理完要复位状态，或误触发稳定判定后重来。
        """
        self.buf.clear()

    def update(self, t: float, gap_px: int) -> None:
        """
        加入一个样本：时间 t（time.monotonic 秒）、间隙 gap_px（像素）。
        会自动丢弃窗口外的旧样本，只保留 [t - window_s, t] 内的数据。

        滑动公式： cutoff = t - window_s
                  while buf[0].t < cutoff: popleft()
        """
        self.buf.append((float(t), int(gap_px)))
        # 丢弃早于 (t - window_s) 的样本，保持滑动窗口
        cutoff = t - self.window_s
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def stats(self) -> Tuple[int, int, int, int, float]:
        """
        基于当前窗口内样本计算统计量。
        :return: (gmin, gmax, span, n, duration_s)
            gmin: 窗口内最小间隙（px）
            gmax: 窗口内最大间隙（px）
            span: gmax - gmin（px），即"抖动幅度"
            n: 样本个数
            duration_s: 窗口内时间跨度（秒）；无样本时返回 0.0
        """
        if not self.buf:
            # 空缓冲直接返回全零，避免下面 min()/max() 抛 ValueError
            return (0, 0, 0, 0, 0.0)
        # 解包成纯 gap 值列表，方便用内置 min/max
        vals = [v for (_, v) in self.buf]
        gmin = min(vals)
        gmax = max(vals)
        span = gmax - gmin                           # 抖动幅度： span = max - min
        n = len(vals)
        # 窗口实际时间跨度（最后一个样本时刻 - 第一个样本时刻）
        # max(0.0, ...) 只是防御性处理，理论上队列中时间严格非递减
        duration = max(0.0, self.buf[-1][0] - self.buf[0][0])
        return (gmin, gmax, span, n, duration)

    def is_stable(self) -> Tuple[bool, int, int, int, float]:
        """
        判断当前窗口内间隙是否"稳定"。
        稳定条件（三者必须同时满足）：
            1) 样本数 n >= min_samples                              —— 数据量够
            2) span < span_thr_px * 2                               —— 抖动不超阈值的 2 倍
               注意这里实际用的是 2*span_thr_px，即"宽容版"阈值；经验值
            3) duration_s >= min(window_s * 0.6, window_s)          —— 累计观察时间够
               min(0.6*W, W) 始终等于 0.6*W；保留 min 写法是为之后想加硬上限时方便
        :return: (ok, gmin, gmax, n, duration_s)，ok 为 True 表示稳定。
        """
        gmin, gmax, span, n, duration = self.stats()
        ok = (
            (n >= self.min_samples)
            and (span < self.span_thr_px * 2)               # span 判定：max-min < 2*阈值
            and (duration >= min(self.window_s * 0.6, self.window_s))
        )
        return ok, gmin, gmax, n, duration
