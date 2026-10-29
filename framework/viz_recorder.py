# -*- coding: utf-8 -*-
# =============================================================================
# framework/viz_recorder.py —— viz 流落盘 + 收尾 H.264 压缩
# -----------------------------------------------------------------------------
# 取代旧 ImageSink (死代码 — 业务路径从未调用 .save()).
#
# 设计目的:
#   1) tee 出 debug_stream encoder 线程已经编码好的 JPEG 字节, 按 run_id × stream 名
#      落到 logs/<run_id>/<stream>/frame_NNNNNN.jpg. 不再让算法/业务代码做 imwrite.
#   2) close_run 触发后, finalizer daemon 启 ffmpeg subprocess 把每个 stream 子目录
#      压成 logs/<run_id>/<stream>.mp4 (libx264), 成功后删源 jpeg, 失败留 .err.txt.
#   3) 自管理保留期 — 三道闸:
#        age 闸:    超过 max_age_s (默认 7d) 的 run_* 整体删
#        count 闸:  最多保留 retain_runs (默认 20) 个 run_*
#        size 闸:   logs/run_*/ 总和 ≤ max_total_mb (默认 500MB), 超出按 mtime 删旧
#      当前、排队等待以及正在收尾的 run_id 永远豁免.
#
# 与 debug_stream 的耦合:
#   - main 启动时调 debug_stream.set_recorder_hook(rec.on_encoded) 挂钩;
#   - 钩子签名 (stream_name, jpeg_bytes) -> None; encoder 线程同步调用;
#   - on_encoded 必须非阻塞 (~5-20ms 写盘) 且永不抛 — 不能拖死 encoder.
#
# 线程模型:
#   - main / encoder 线程        : open_run / close_run / on_encoded
#   - finalizer daemon × 1        : 从队列拉 run_id → 等 1s in-flight 写完 → ffmpeg →
#                                    清源 → retention 扫描
#
# 不引入新 pip 依赖. ffmpeg 是系统二进制 (subprocess); 已确认 x5 (4.4.2) + Orin (8.0)
# 都有.
# =============================================================================
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Set, Tuple


# 默认保留策略 — main.py 构造时可覆盖.
DEFAULT_RETAIN_RUNS = 20             # 数量闸
DEFAULT_MAX_AGE_S = 7 * 24 * 60 * 60 # 7 天 (与 logger_utils.LOG_MAX_AGE_S 一致)
DEFAULT_MAX_TOTAL_MB = 500           # 总和上限; 超出按 mtime 升序删旧
DEFAULT_FRAMERATE = 5                # ffmpeg 编码帧率; ≈ detector 实际 fps (5-6)

# close_run 入队到 finalizer 实际 ffmpeg 之间的延迟 — 让 encoder 线程把 in-flight 的 JPEG
# 写完. encoder 线程最长延迟 = queue.get timeout (0.5s) + 编码 (~10ms) + 写 (~20ms) ≈ 0.6s.
# 留 1s 是安全余量.
FINALIZE_PRE_DELAY_S = 1.0

# ffmpeg subprocess 上限. 30s run @ 5fps = 150 frame, libx264 veryfast 在 x5 ARM 上 ≈ 3-5s
# 完成. 120s 给极端情况兜底, 防超长 run / SD 写慢导致 ffmpeg 卡住.
FFMPEG_TIMEOUT_S = 120


class VizRecorder:
    """viz 流落盘 + per-run H.264 收尾压缩, 替代旧 ImageSink.

    用法 (与旧 ImageSink 接口部分兼容: 同 open_run / close_run; 新增 on_encoded):
        rec = VizRecorder(base_dir='logs', logger=logger)
        rec.start()
        debug_stream.set_recorder_hook(rec.on_encoded)
        # ... 业务 (actions): rec.open_run(run_id) / rec.close_run()
        rec.stop(drain_timeout=15)
    """

    def __init__(
        self,
        base_dir: str = 'logs',
        retain_runs: int = DEFAULT_RETAIN_RUNS,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        max_total_mb: float = DEFAULT_MAX_TOTAL_MB,
        framerate: int = DEFAULT_FRAMERATE,
        logger: Any = None,
    ) -> None:
        # absolute 化避免后续 cwd 变化影响. logs 目录里同时住着 log_*.txt (Asynclogger)
        # 和 run_*/ (本模块) — 我们只动 run_* 前缀的, log_* 由 Asynclogger 自己管.
        self._base_dir = os.path.abspath(base_dir)
        self._retain_runs = max(1, int(retain_runs))
        self._max_age_s = max(0.0, float(max_age_s))
        self._max_total_bytes = int(max_total_mb * 1024 * 1024)
        self._framerate = max(1, int(framerate))
        self._logger = logger

        # ---- 当前 run 状态 ----
        # _current_run_id: 主线程 open_run 设 / close_run 清 None. encoder 线程 (debug_stream
        # encoder loop) 通过 on_encoded 读快照; 读到 None 即丢帧 (run 未开始 / 已结束).
        # 生命周期锁让关闭入队、进入收尾和 retention 删除之间没有保护空窗.
        # encoder 只读取当前 run 的快照, 不在写盘期间持有此锁.
        self._run_lock = threading.RLock()
        self._current_run_id: Optional[str] = None

        # _counters: (run_id, stream_name) → 当前帧号. on_encoded 加锁递增. 用复合键
        # 允许多个 encoder 线程并发写同 run_id 不同 stream, 也允许下一个 run 的同名 stream
        # 从 1 重新开始.
        self._counters: Dict[Tuple[str, str], int] = {}
        self._counter_lock = threading.Lock()

        # _known_dirs: 已 mkdir 过的 (run_id, stream) 集合, 用来跳过每帧的 makedirs 调用.
        # open_run 不清 — 即使收到 close_run 之后的 stale 帧也无害 (写到旧 run 目录, finalize
        # 已经处理过该目录则被忽略; 还在 finalize 队列中则属于"已统计入帧序号"的合法写).
        self._known_dirs: Set[Tuple[str, str]] = set()
        self._dir_lock = threading.Lock()

        # ---- finalize daemon ----
        # finalize_queue: close_run put rid, daemon get rid → 跑 ffmpeg.
        self._finalize_queue: queue.Queue = queue.Queue()

        # 从 close_run 入队起即保护, 包括等候其他 run 收尾的整个时间段.
        self._pending_finalize_rids: Set[str] = set()
        # dequeue 到收尾完成期间保持保护, 包括 in-flight 写入等待阶段.
        self._finalizing_rids: Set[str] = set()

        # _current_ffmpeg: 当前在跑的 ffmpeg subprocess. stop() 超时时 terminate.
        self._current_ffmpeg: Optional[subprocess.Popen] = None
        self._current_ffmpeg_lock = threading.Lock()

        self._stop_event = threading.Event()
        # 正常 stop 排空队列；超时后独立取消，保留未完成任务与源 JPEG。
        self._abort_finalize = threading.Event()
        self._finalizer_thread: Optional[threading.Thread] = None

    # =========================================================================
    # 生命周期
    # =========================================================================

    def start(self) -> None:
        if self._finalizer_thread is not None and self._finalizer_thread.is_alive():
            return
        os.makedirs(self._base_dir, exist_ok=True)
        self._stop_event.clear()
        self._abort_finalize.clear()
        self._finalizer_thread = threading.Thread(
            target=self._finalizer_loop, name='VizRecorder.finalize', daemon=True,
        )
        self._finalizer_thread.start()

    def stop(self, drain_timeout: float = 15.0) -> None:
        """触发 finalizer daemon 退出, 等其把队列中的 finalize 任务做完.

        drain_timeout 到期后取消后续任务并 terminate/kill 当前 ffmpeg；源 JPEG
        和未完成任务保留。无法及时退出的线程仍保留引用，避免重复启动。
        """
        # 若 shutdown 时还有 run 没 close (e.g. SIGTERM 打断 dosing), 入队它做个尾收
        if self._current_run_id is not None:
            self.close_run()
        self._stop_event.set()
        if self._finalizer_thread is not None:
            self._finalizer_thread.join(timeout=drain_timeout)
            if self._finalizer_thread.is_alive():
                # 与 Popen 共用锁：取消一旦生效，不能再启动下一条 stream/run。
                with self._current_ffmpeg_lock:
                    self._abort_finalize.set()
                    if self._current_ffmpeg is not None:
                        try: self._current_ffmpeg.terminate()
                        except Exception: pass
                self._finalizer_thread.join(timeout=2.0)
                if self._finalizer_thread.is_alive():
                    with self._current_ffmpeg_lock:
                        if self._current_ffmpeg is not None:
                            try: self._current_ffmpeg.kill()
                            except Exception: pass
                    self._finalizer_thread.join(timeout=2.0)
            if not self._finalizer_thread.is_alive():
                self._finalizer_thread = None

    # =========================================================================
    # 公开 API (主线程)
    # =========================================================================

    def open_run(self, run_id: str) -> str:
        """开新 run 子目录, 触发 retention 扫描. 返回目录绝对路径."""
        if not run_id:
            raise ValueError('open_run: run_id is required')
        run_dir = os.path.join(self._base_dir, run_id)
        with self._run_lock:
            os.makedirs(run_dir, exist_ok=True)
            self._current_run_id = run_id
        # 新 run 开始时做 retention — 在写新数据前清旧, 避免临界期磁盘满.
        # _counters / _known_dirs 不清: 旧 run 的 stale 帧 (如有) 会被 _current_run_id 闸拦掉,
        # 即使误穿一两个 (race window) 也只是写到刚被收的 run 目录, 无害.
        self._retention_sweep()
        self._log('info', f'[viz-rec] open_run -> {run_dir}')
        return run_dir

    def close_run(self) -> None:
        """关 run + 入队 finalize. fire-and-forget — 主线程不阻塞."""
        with self._run_lock:
            rid = self._current_run_id
            if rid is None:
                return
            # 先保护待收尾 run, 再解除 current; retention 看不到未保护的中间状态.
            self._pending_finalize_rids.add(rid)
            self._current_run_id = None
            try:
                self._finalize_queue.put_nowait(rid)
            except queue.Full:
                # 默认无界; 兜底恢复会话, 保留下一次 close/stop 重试机会.
                self._pending_finalize_rids.discard(rid)
                self._current_run_id = rid
                self._log('error', f'[viz-rec] close_run: finalize queue full, keep {rid}')

    # =========================================================================
    # 钩子 (debug_stream encoder 线程同步调用)
    # =========================================================================

    def on_encoded(self, name: str, jpeg_bytes: bytes) -> None:
        """encoder loop 钩子: JPEG 编码后直写磁盘. 同步, ~5-20ms.

        异常吃掉 — encoder 线程不能因为我们 die. encoder 是 daemon, 死了 MJPEG 推流就停了.
        """
        rid = self._current_run_id  # 一次性快照; close_run 之后此处仍可能读到旧值, 那一帧合法落入旧 run
        if rid is None or not jpeg_bytes:
            return
        key = (rid, name)
        stream_dir = os.path.join(self._base_dir, rid, name)
        # mkdir 缓存命中后跳过, 省掉每帧的 stat 调用 (大约 0.5ms/次, 5 stream × 5fps = 12ms/s)
        if key not in self._known_dirs:
            try:
                os.makedirs(stream_dir, exist_ok=True)
            except OSError as e:
                self._log('warning', f'[viz-rec] mkdir failed: {stream_dir} {e}')
                return
            with self._dir_lock:
                self._known_dirs.add(key)
        # 帧号: 锁内递增. 5 encoder 线程并发不冲突 (各自有不同 stream_name).
        with self._counter_lock:
            counter = self._counters.get(key, 0) + 1
            self._counters[key] = counter
        path = os.path.join(stream_dir, f'frame_{counter:06d}.jpg')
        try:
            # 简单 open+write — 部分写入的 .jpg 被 ffmpeg 解码会跳过 (corrupted JPEG warning),
            # ffmpeg image2 pattern_type=glob 容忍序号不连续, 整体不影响其他帧.
            with open(path, 'wb') as f:
                f.write(jpeg_bytes)
        except OSError as e:
            self._log('warning', f'[viz-rec] write failed: {path} {e}')

    # =========================================================================
    # finalizer daemon
    # =========================================================================

    def _finalizer_loop(self) -> None:
        """从 _finalize_queue 拉 run_id, 编码 + 清源 + retention. 退出条件: stop ∧ 队列空."""
        while (not self._abort_finalize.is_set()
               and (not self._stop_event.is_set() or not self._finalize_queue.empty())):
            try:
                rid = self._finalize_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._run_lock:
                # 在同一把锁下交接保护, 等待期间也已属于 finalizing.
                self._finalizing_rids.add(rid)
                self._pending_finalize_rids.discard(rid)
            try:
                # 留窗口给 encoder 线程把 in-flight 编码完写完
                time.sleep(FINALIZE_PRE_DELAY_S)
                if not self._abort_finalize.is_set():
                    self._finalize_run(rid)
            except Exception as exc:
                self._log('error', f'[viz-rec] finalize({rid}) raised: {exc}')
            finally:
                with self._run_lock:
                    self._finalizing_rids.discard(rid)
                    if self._abort_finalize.is_set():
                        # 保留保护及任务，以便之后 start() 重试；取消时不做 retention。
                        self._pending_finalize_rids.add(rid)
                        self._finalize_queue.put_nowait(rid)
                self._finalize_queue.task_done()
                # 收尾完成后再应用上限; 之前超限的待收尾 run 始终受保护.
                if not self._abort_finalize.is_set():
                    self._retention_sweep()

    def _finalize_run(self, rid: str) -> None:
        """把 logs/<rid>/<stream>/*.jpg 压成 logs/<rid>/<stream>.mp4; 单帧 stream 直接搬出."""
        run_dir = os.path.join(self._base_dir, rid)
        if not os.path.isdir(run_dir):
            self._log('warning', f'[viz-rec] finalize: run_dir missing: {run_dir}')
            return
        streams_done = 0
        streams_failed = 0
        try:
            entries = os.listdir(run_dir)
        except OSError as e:
            self._log('warning', f'[viz-rec] finalize listdir failed: {run_dir} {e}')
            return
        for name in entries:
            if self._abort_finalize.is_set():
                break
            stream_dir = os.path.join(run_dir, name)
            if not os.path.isdir(stream_dir):
                continue
            try:
                jpgs = sorted(
                    f for f in os.listdir(stream_dir)
                    if f.startswith('frame_') and f.endswith('.jpg')
                )
            except OSError:
                continue
            if not jpgs:
                # 空目录, 清掉
                shutil.rmtree(stream_dir, ignore_errors=True)
                continue
            if len(jpgs) == 1:
                # 单帧 stream (e.g. scale_refine_profile 一次性 lock 时推) — 不值得 ffmpeg,
                # 直接搬到 run_dir 根下保留 jpg.
                src = os.path.join(stream_dir, jpgs[0])
                dst = os.path.join(run_dir, f'{name}.jpg')
                try:
                    shutil.move(src, dst)
                    shutil.rmtree(stream_dir, ignore_errors=True)
                    streams_done += 1
                except OSError as e:
                    self._log('warning', f'[viz-rec] single-frame move failed: {name} {e}')
                    streams_failed += 1
                continue
            # 多帧 → ffmpeg
            ok = self._ffmpeg_encode(stream_dir, run_dir, name)
            if ok and not self._abort_finalize.is_set():
                shutil.rmtree(stream_dir, ignore_errors=True)
                streams_done += 1
            else:
                # 失败: 留源 jpeg + .err.txt; 不删 stream_dir
                streams_failed += 1
        self._log(
            'info',
            f'[viz-rec] finalize({rid}) done={streams_done} failed={streams_failed}',
        )

    def _ffmpeg_encode(self, src_dir: str, out_dir: str, name: str) -> bool:
        """libx264 编码 src_dir/frame_*.jpg → out_dir/<name>.mp4. True=成功."""
        # .partial 临时名, 成功后 rename — 防下载端在编码中途打包到半成品.
        partial = os.path.join(out_dir, f'{name}.mp4.partial')
        final = os.path.join(out_dir, f'{name}.mp4')
        cmd = [
            'ffmpeg',
            '-y',

            '-framerate',
            str(self._framerate),

            '-i',
            os.path.join(src_dir, 'frame_%06d.jpg'),

            '-vf',
            'scale=trunc(iw/2)*2:trunc(ih/2)*2',

            '-c:v',
            'libx264',

            '-preset',
            'veryfast',

            '-crf',
            '23',

            '-pix_fmt',
            'yuv420p',

            '-movflags',
            '+faststart',

            '-f',
            'mp4',

            partial,
        ]
        try:
            with self._current_ffmpeg_lock:
                if self._abort_finalize.is_set():
                    return False
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                self._current_ffmpeg = proc
        except FileNotFoundError:
            self._log('error', '[viz-rec] ffmpeg binary not found in PATH')
            return False
        except Exception as e:
            self._log('error', f'[viz-rec] ffmpeg spawn raised: {e}')
            return False
        try:
            _stdout, stderr_bytes = proc.communicate(timeout=FFMPEG_TIMEOUT_S)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            self._log('error', f'[viz-rec] ffmpeg timeout: {name}')
            try: proc.kill()
            except Exception: pass
            try: proc.communicate(timeout=5)
            except Exception: pass
            rc = -1
            stderr_bytes = b'(timeout)'
        finally:
            with self._current_ffmpeg_lock:
                self._current_ffmpeg = None
        if rc == 0 and not self._abort_finalize.is_set() and os.path.exists(partial):
            try:
                # atomic on POSIX — download 扫到 .mp4 必然是完成态
                os.rename(partial, final)
                return True
            except OSError as e:
                self._log('warning', f'[viz-rec] rename failed: {name} {e}')
                return False
        # 失败 — 留 stderr 到 .err.txt 便于排查
        err_msg = stderr_bytes.decode('utf-8', errors='replace') if stderr_bytes else ''
        try:
            with open(os.path.join(out_dir, f'{name}.err.txt'), 'w', encoding='utf-8') as f:
                f.write(f'ffmpeg rc={rc}\n{err_msg}\n')
        except OSError:
            pass
        try: os.remove(partial)
        except OSError: pass
        self._log('warning', f'[viz-rec] ffmpeg failed: {name} rc={rc}')
        return False

    # =========================================================================
    # retention
    # =========================================================================

    def _retention_sweep(self) -> None:
        """扫描与删除期间锁住生命周期, 保护当前、排队和正在收尾的 run."""
        with self._run_lock:
            self._retention_sweep_locked()

    def _retention_sweep_locked(self) -> None:
        """三道闸顺序: age → count → size. 调用方持有 _run_lock."""
        try:
            entries = os.listdir(self._base_dir)
        except OSError:
            return
        runs = []
        for name in entries:
            if not name.startswith('run_'):
                continue
            p = os.path.join(self._base_dir, name)
            if not os.path.isdir(p):
                continue
            try:
                mtime = os.path.getmtime(p)
                size = self._dir_size(p)
            except OSError:
                continue
            runs.append((mtime, size, name, p))
        if not runs:
            return
        runs.sort()  # mtime ascending (oldest first)
        now = time.time()
        # 保护集合与目录删除共用生命周期锁, 避免快照过期后误删.
        protected = self._pending_finalize_rids | self._finalizing_rids
        if self._current_run_id is not None:
            protected.add(self._current_run_id)

        # 1) age 闸 — 删超龄 (豁免跳过)
        keep = []
        for r in runs:
            mtime, size, name, p = r
            if name in protected:
                keep.append(r)
            elif self._max_age_s > 0 and (now - mtime) > self._max_age_s:
                self._rm_run(p)
            else:
                keep.append(r)

        # 2) count 闸 — 总数 (含当前 run) 超 retain_runs 时按 mtime 升序删非豁免
        # 未完成 run 也计入总数, 但不删除; 等待收尾完成后再收紧到上限.
        if len(keep) > self._retain_runs:
            excess = len(keep) - self._retain_runs
            deleted = 0
            new_keep = []
            for r in keep:  # 已按 mtime 升序
                if deleted < excess and r[2] not in protected:
                    self._rm_run(r[3])
                    deleted += 1
                else:
                    new_keep.append(r)
            keep = new_keep

        # 3) size 闸 — 总和超 cap 按 mtime 升序删非豁免
        total = sum(r[1] for r in keep)
        for r in keep:  # 已按 mtime 升序 — 最旧先删
            if total <= self._max_total_bytes:
                break
            if r[2] in protected:
                continue
            self._rm_run(r[3])
            total -= r[1]

    def _rm_run(self, path: str) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            self._log('warning', f'[viz-rec] retention rm failed: {path} {e}')

    @staticmethod
    def _dir_size(path: str) -> int:
        """递归 du. 单次 os.walk; 单文件 stat 失败计 0 不抛."""
        total = 0
        try:
            for dirpath, _dirnames, filenames in os.walk(path):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    # =========================================================================
    # 日志
    # =========================================================================

    def _log(self, level: str, msg: str) -> None:
        if self._logger is None:
            print(msg)
            return
        fn = getattr(self._logger, level, None)
        if callable(fn):
            try: fn(msg)
            except Exception: pass
