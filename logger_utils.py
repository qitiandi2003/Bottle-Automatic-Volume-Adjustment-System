# -*- coding: utf-8 -*-
# =============================================================================
# logger_utils.py —— 轻量级异步日志模块
# -----------------------------------------------------------------------------
# 设计目的：
#   检测主循环对实时性敏感（每一帧都要做几何识别/液面检测/控制通道收发），如果
#   在主线程里直接 open/write 磁盘，写盘的 I/O 抖动会拖慢帧率。因此这里抽出一条
#   守护线程专门负责日志落盘 —— 业务线程只做"入队"，真正的 open/write 由后台
#   线程串行消费，天然避免多线程写同一文件的锁问题。
# 要点：
#   - 生产者/消费者队列模型：info()/warning()/error() 把字符串丢进 Queue 即返回；
#     run() 从队列拉取并写入 logs/log_YYYYMMDD_HHMM.txt 文件。
#   - 启动时清理 24h 以前的旧日志，防止磁盘无限增长。
#   - 线程设置为 daemon=True，主程序退出时线程自动回收，stop() 则会把队列尾巴
#     消费完再 join。
# =============================================================================

import threading               # 后台日志线程：继承 Thread
from datetime import datetime  # 日志时间戳、日志文件命名
import queue                   # 线程安全 FIFO，作为生产者-消费者的缓冲
import sys                     # run() 里把意外异常打到 stderr
import os                      # 目录/路径操作、mtime 获取
import time                    # 清理旧日志时拿当前 Unix 时间戳

# 长跑保留期: 7 天. cleanup 删除 mtime 早于此的 log_*.txt.
LOG_MAX_AGE_S = 7 * 24 * 60 * 60
# 守护线程的清理 tick 间隔: 1 小时. 进程不重启时仍能持续回收旧日志.
CLEANUP_INTERVAL_S = 60 * 60


class Asynclogger(threading.Thread):
    """
    后台日志写入线程。
    负责从队列中取出日志消息并写入文件/控制台。

    典型用法：
        lg = Asynclogger("MyNode")   # 构造（此时仅入队一条初始化消息，尚未启动线程）
        lg.start()                   # 启动后台消费线程
        lg.info("hello")             # 主线程只负责生产
        ...
        lg.stop()                    # 收尾：等后台把队列写完
    """
    def __init__(self, name="AVC", print_to_console=True):
        """
        参数：
            name: 节点名，会作为日志行前缀的一部分，便于多节点日志汇总时区分来源。
                  默认 "AVC" = auto volume control（本项目代号）。
            print_to_console: 是否在写盘之外同时把日志打到标准输出，开发时方便观察。
        """
        super().__init__(daemon=True)       # 设置为守护线程，主程序退出时自动结束
        # __file__ 指向本模块绝对路径；dirname 取所在目录 → 日志目录放在项目同级的 logs/
        project_dir = os.path.dirname(os.path.abspath(__file__))
        self._logs_dir = os.path.join(project_dir, "logs")
        os.makedirs(self._logs_dir, exist_ok=True)   # 目录不存在就创建，存在就忽略
        # 启动时立即跑一次清理（保留期 LOG_MAX_AGE_S）；之后由 run() 守护线程按 CLEANUP_INTERVAL_S 周期 tick.
        self._cleanup_old_logs(self._logs_dir, max_age_seconds=LOG_MAX_AGE_S)
        # 下一次 cleanup 的截止时间戳；run() 每轮检测是否到点.
        self._next_cleanup_ts = time.time() + CLEANUP_INTERVAL_S
        # 文件名改为按"当前日期"动态生成（log_YYYYMMDD.txt）；每次写盘时重算路径，
        # 跨日自动切到新文件，老文件 mtime 凍結 → 滿 LOG_MAX_AGE_S 後被 cleanup 回收.

        # queue.Queue(-1) 表示无界队列：生产端永不阻塞；代价是消费端卡死会吃内存
        self.log_queue = queue.Queue(-1)
        self.running = True            # 运行标志位；stop() 会将其置 False
        self.node_name = name          # 缓存节点名，供 info/warning/error 拼接前缀

        self.print_to_console = print_to_console  # 是否同时输出到控制台

        # 构造函数里就入队一条初始化消息（线程还没 start，所以现在只是"排队"）
        self.info("Logger thread initialized.")

    def _current_log_path(self):
        """按当前日期返回今日日志文件路径. run() 每次写盘前调,跨日自然切档."""
        return os.path.join(self._logs_dir, f"log_{datetime.now().strftime('%Y%m%d')}.txt")

    def get_logs_dir(self):
        """供外部(HTTP 下载端点)读取日志目录绝对路径."""
        return self._logs_dir

    def _cleanup_old_logs(self, logs_dir, max_age_seconds):
        """
        删除日志目录中超过保留时长的日志文件。

        规则：
            只处理形如 log_*.txt 的文件（避免误删其它文件）。
            判定公式： age = now_ts - os.path.getmtime(file)
                      if age > max_age_seconds: 删除
        """
        now_ts = time.time()            # Unix 时间戳（秒），基准用于所有差值比较
        for filename in os.listdir(logs_dir):
            # 仅清理当前项目日志文件，避免误删其他文件
            # 同时满足前缀 log_ 与后缀 .txt 才认为是本项目写的日志
            if not (filename.startswith("log_") and filename.endswith(".txt")):
                continue
            file_path = os.path.join(logs_dir, filename)
            if not os.path.isfile(file_path):   # 目录/软链接跳过，只删常规文件
                continue
            try:
                if (now_ts - os.path.getmtime(file_path)) > max_age_seconds:
                    os.remove(file_path)        # 超期，物理删除
            except Exception:
                # 清理失败不影响主流程
                # 例如文件被其他进程占用、权限不足等，构造函数不应因清理失败而崩溃
                pass

    def run(self):
        """
        线程主循环：不断从队列取消息并处理。

        退出条件：running=False 且 队列已空
            —— 确保 stop() 被调用后，剩余未写的日志也能落盘（不丢日志）。
        """
        while self.running or not self.log_queue.empty():
            try:
                # 设置 timeout 防止死锁，允许每隔一段时间检查 running 状态
                # 0.5 秒是经验值：stop() 最多等半秒就能被检测到，既不忙等也不过慢
                record = self.log_queue.get(timeout=0.5)
                # 每条消息独立 open/append/close：实现简单、稳，一旦进程崩溃最多丢 0 条；
                # 高频日志场景下可改为"长开 fd + flush"，当前并发量不需要.
                # 文件名按今日日期动态算 → 跨日自动切档,长跑场景不会单档膨胀无上限.
                with open(self._current_log_path(), 'a', encoding='utf-8') as f:
                    f.write(record + '\n')
                if self.print_to_console:
                    print(record)
            except queue.Empty:
                # timeout 触发但队列仍空 → 不 continue, 让控制流落到 cleanup tick.
                pass
            except Exception as e:
                # 任何意外（磁盘满、权限变、编码错误）都打到 stderr，保证日志线程不自杀
                print(f"Logging thread error: {e}", file=sys.stderr)
            # ---- 周期清理 tick ----
            # 长跑场景: 进程不重启,只靠构造期清理一次会让 mtime > MAX_AGE 的旧档堆积.
            # 这里每 CLEANUP_INTERVAL_S 跑一次,代价是 listdir + 若干 stat,毫秒级.
            # 失败被吃掉,绝不让 cleanup 异常杀掉日志线程.
            if time.time() >= self._next_cleanup_ts:
                try:
                    self._cleanup_old_logs(self._logs_dir, max_age_seconds=LOG_MAX_AGE_S)
                except Exception as e:
                    print(f"Logging cleanup error: {e}", file=sys.stderr)
                self._next_cleanup_ts = time.time() + CLEANUP_INTERVAL_S

    def info(self, message):
        """
        记录信息级别日志。
        格式： [INFO][节点名][HH:MM:SS] message
        """
        timestamp = datetime.now().strftime('%H:%M:%S')     # 只到秒，日志行窄一点
        log_record = f"[INFO][{self.node_name}][{timestamp}] {message}"
        self.log_queue.put(log_record)                      # 入队后立即返回，不阻塞调用方

    def warning(self, message):
        """
        记录警告级别日志。
        语义：可恢复的异常分支、偶发重连、非致命问题。
        """
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_record = f"[WARNING][{self.node_name}][{timestamp}] {message}"
        self.log_queue.put(log_record)

    def error(self, message):
        """
        记录错误级别日志。
        语义：功能受损或流程中断，如相机断连、socket 发送持续失败。
        """
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_record = f"[ERROR][{self.node_name}][{timestamp}] {message}"
        self.log_queue.put(log_record)

    def stop(self):
        """
        停止日志线程，等待其处理完队列中的消息。

        调用顺序：主程序收尾 → lg.stop() 阻塞，直到后台线程 join 完成返回。
        """
        self.running = False        # 将 while 退出条件翻为 False；余量消费完线程自然结束
        timestamp = datetime.now().strftime('%H:%M:%S')
        # 这里直接 print 而不走队列：因为此时线程已经停了，若再入队反而会被丢弃；
        # 用户最关心的"日志保存在哪"必须立即可见，故直接写 stdout.
        # 路径用 _current_log_path() 现算 —— 跨日运行时 stop 时刻的日期决定输出哪个文件名.
        print(f"[INFO][{self.node_name}][{timestamp}] 日志保存至 ： {self._current_log_path()}")
        self.join()  # 等待线程结束
