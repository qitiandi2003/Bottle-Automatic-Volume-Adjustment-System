# -*- coding: utf-8 -*-
# =============================================================================
# rt_util.py —— Runtime 工具函数集合
# -----------------------------------------------------------------------------
# 这是系统最底层的通用工具文件，不依赖任何项目内的其他模块，只依赖标准库：
#   - _env_flag               ：读取 bool 型环境变量
#   - print_error/print_info  ：带颜色的 stderr/stdout 打印
#   - print_info_throttle     ：同 key 节流版打印（避免日志刷屏）
#   - warn_throttled          ：节流告警
#   - tcp_connect             ：建立（重建）TCP 客户端连接
#   - send_detect_packet      ：把检测结果打成协议包并 UDP/TCP 发送出去
#   - STATUS_*                ：7 种系统状态枚举 (新框架 to_wire_status 映射出口)
# =============================================================================
import os
import socket              # send_detect_packet / tcp_connect 使用
import sys                 # stderr 打印


def _env_flag(name: str, default: bool) -> bool:
    """
    读取字符串型环境变量，按常见"真/假"语义转成 bool。
    规则：未设置 → default；否则去空格+转小写，命中 {"0","false","no","off"} 视为 False，其它视为 True。
    用法：  EXP_BOOT_ENABLED = _env_flag('EXP_BOOT_ENABLED', True)
    """
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


# ---------- ANSI 颜色转义：仅在支持的终端上显示，不支持时会看到原始转义符 ----------
_RED   = "\033[31m"       # 红色：错误
_CYAN  = "\033[36m"       # 青色：信息
_RESET = "\033[0m"        # 重置到默认颜色


def print_error(msg: str):
    """以红色输出错误信息到 stderr。"""
    # 把 msg 夹在 _RED...._RESET 里；file=sys.stderr 确保不会和正常日志混流
    print(f"{_RED}{msg}{_RESET}", file=sys.stderr)


def print_info(msg: str):
    """以青色输出信息到 stdout。"""
    print(f"{_CYAN}{msg}{_RESET}")


def print_info_throttle(msg: str, gap_s: float = 2.0, *, key: str | None = None):
    """限流版 print_info，同一 key 在 gap_s 秒内只输出一次。
    key 默认取 msg 中第一个 [] 标签（如 '[liq-lock]'），相同 key 共享节流。

    应用场景：液面锁定、重连、常驻状态类信息在循环里会被高频触发，用这个函数
    可以避免同一条消息刷屏。实现方式是函数对象上挂一个 dict 作为状态。
    """
    import time    # 延迟导入：极少被调用的函数，避免模块 import 时间被拖慢
    now = time.monotonic()          # 单调时钟（不会因系统时间修改而回跳）
    if key is None:
        # key 默认算法：从 msg 中取首个 [] 内的文字作为节流键
        _s = msg.find('[')
        _e = msg.find(']', _s + 1) if _s >= 0 else -1
        key = msg[_s:_e + 1] if _e > _s else msg
    # _last_ts 存在函数对象属性上，等价于用一个静态字典缓存各 key 的上次打印时间
    last = print_info_throttle._last_ts.get(key, 0.0)
    if now - last >= gap_s:         # 距上次超过 gap_s 秒才真正打印
        print_info(msg)
        print_info_throttle._last_ts[key] = now   # 更新"上次打印时间"

# 给函数加个 dict 属性当作静态变量（Python 里常见的轻量缓存技巧）
print_info_throttle._last_ts = {}


def warn_throttled(msg: str, gap_s: float = 5.0):
    """限流打印告警（红色），同一消息在 gap_s 秒内只输出一次。

    与 print_info_throttle 不同的是这里只维护一个全局时间戳（不分 key），
    因此所有告警共用同一节流窗口，适合偶发性批量失败（比如 TCP 发送）。
    """
    import time
    now = time.monotonic()
    if now - warn_throttled._last_ts >= gap_s:
        print_error(msg)
        warn_throttled._last_ts = now

warn_throttled._last_ts = 0.0   # 首次调用时视为"从未打印"

def tcp_connect(addr, control_reader, old_sock=None):
    """关闭旧连接（若存在），新建 TCP 连接并通知 control_reader。返回新 socket。

    参数：
        addr            : (host, port) tuple，被连接方
        control_reader  : 控制通道读取器，负责在新 socket 上监听上位机指令
        old_sock        : 前一次使用的 socket；非 None 时先关掉
    """
    # 若之前的 socket 还在，先 close。即便 close 出错也忽略，因为目标就是丢弃它
    if old_sock is not None:
        try:
            old_sock.close()
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
    s.settimeout(2.0)          # 连接阶段超时 2s，防止网络异常时长时间阻塞
    s.connect(addr)
    s.settimeout(None)         # 连接成功后恢复阻塞/无超时，让业务层自己控制
    try:
        # 告诉 control_reader "连接变了，请在新 socket 上重新启动接收循环"
        control_reader.update_socket(s)
    except Exception as _e:
        print_error(f"[ctrl] reader start failed: {_e}")
    return s


def send_detect_packet(task_status, scale_gap_px, liquid_to_scale_px=0, *, force=False,
                       config, tx_sock, run_state, control_reader):
    """发送检测结果数据包，返回（可能已更新的）tx_sock。
    std: 7B  <BBHHB = [AA, status, gap(le,u16), l2s(le,u16), 55]
    cpp: 8B         = [AA, status, gap(be,u16), l2s_hi, l2s_lo, 55, 00]
    gap/l2s 无效数值用 10000 占位。

    参数释义：
        task_status         : 系统状态码（STATUS_* 枚举），占 1 字节
        scale_gap_px        : 刻度间距(单位：像素)，表征测量精度 / 标尺对齐情况
        liquid_to_scale_px  : 液面到目标刻度的有符号距离(像素)
        force               : True 表示忽略"run_state 未启动"而强制发包（用于开机握手或停止确认）
        config              : rt_config 模块引用（里面有 TX_* 常量）
        tx_sock             : 已有的发送 socket；UDP 可复用，TCP 断线会自动重连
        run_state           : 运行状态对象，is_running() 判断任务是否已启动
        control_reader      : 控制通道读取器；新建 TCP 时要把新 socket 转过去
    """
    import struct as _struct          # 局部导入避免顶层耦合
    # 未启用发送或目标地址缺失 → 直接返回，无事发生
    if not config.TX_ENABLED or config.TX_ADDR is None:
        return tx_sock
    # 非 force 且任务未开始 → 不发：避免闲时对外刷包
    if (not force) and (not run_state.is_running()):
        return tx_sock

    status_byte = int(task_status) & 0xFF   # 只保留 1 字节，防止高位溢出

    def _to_u16(v, default=10000):
        """把 v 安全转成 uint16；None 或转换失败时回落到 default。
        约定 10000 作为"无效值占位"，上位机看到此值即判定本字段暂不可用。"""
        if v is None:
            return default
        try:
            return int(v) & 0xFFFF
        except Exception:
            return default

    gap = _to_u16(scale_gap_px, 10000)           # 刻度间距像素 → u16
    l2s = _to_u16(liquid_to_scale_px, 10000)     # 液面到刻度距离(px) → u16

    # 以下两段把调试用的"原始签名值"保留下来，供 verbose 日志打印，不影响协议字节
    try:
        gap_s_dbg = None if scale_gap_px is None else int(scale_gap_px)
    except Exception:
        gap_s_dbg = None
    try:
        l2s_s_dbg = None if liquid_to_scale_px is None else int(liquid_to_scale_px)
    except Exception:
        l2s_s_dbg = None

    if config.TX_LAYOUT == 'cpp':
        # C++ 端期望的布局：8 字节大端 + 末尾保留 0x00
        # 帧格式： [0]=0xAA  [1]=status  [2..3]=gap(BE)  [4..5]=l2s(BE)  [6]=0x55  [7]=0x00
        b = bytearray(8)
        b[0] = 0xAA                      # 帧头
        b[1] = status_byte               # 状态
        b[2] = (gap >> 8) & 0xFF         # gap 高字节：公式  hi = (gap >> 8) & 0xFF
        b[3] = gap & 0xFF                # gap 低字节：公式  lo =  gap       & 0xFF
        b[4] = (l2s >> 8) & 0xFF         # l2s 高字节
        b[5] = l2s & 0xFF                # l2s 低字节
        b[6] = 0x55                      # 帧尾
        b[7] = 0x00                      # 对齐占位
        pkt = bytes(b)
    else:
        # 标准 7 字节小端布局：一次 struct.pack 搞定
        # 格式 '<BBHHB'：小端、u8、u8、u16、u16、u8
        pkt = _struct.pack('<BBHHB', 0xAA, status_byte, gap, l2s, 0x55)

    try:
        if config.TX_PROTO == 'udp':
            # UDP：无连接，直接建 socket 就能 sendto
            if tx_sock is None:
                tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx_sock.sendto(pkt, config.TX_ADDR)
            if config.TX_VERBOSE:
                print(f"[tx] sent udp state={status_byte} gap={gap}(s={gap_s_dbg}) l2s={l2s}(s={l2s_s_dbg}) -> {config.TX_ADDR}")
                print(f"[tx] bytes udp: {pkt.hex(' ')}")
        else:
            # TCP：首次或断线时调用 tcp_connect 重建
            if tx_sock is None:
                tx_sock = tcp_connect(config.TX_ADDR, control_reader, tx_sock)
            tx_sock.sendall(pkt)   # 保证整段写入，而非 send() 只写部分
            if config.TX_VERBOSE:
                print(f"[tx] sent tcp state={status_byte} gap={gap}(s={gap_s_dbg}) l2s={l2s}(s={l2s_s_dbg}) -> {config.TX_ADDR}")
                print(f"[tx] bytes tcp: {pkt.hex(' ')}")
    except Exception as _e:
        # 发送出错：TCP 下尝试一次立即重连并重发（一次性重试，避免无限循环）
        if config.TX_PROTO == 'tcp':
            try:
                tx_sock = tcp_connect(config.TX_ADDR, control_reader, tx_sock)
                tx_sock.sendall(pkt)
                if config.TX_VERBOSE:
                    print(f"[tx] sent proto=tcp(reconnect) layout={config.TX_LAYOUT} state={status_byte} gap={gap}(s={gap_s_dbg}) l2s={l2s}(s={l2s_s_dbg}) -> {config.TX_ADDR} bytes={pkt.hex(' ')}")
                return tx_sock
            except Exception as _e2:
                # 连第二次重连都失败：节流打一条告警
                warn_throttled(f"[warn] tx tcp reconnect failed: {_e2}")
        # UDP 失败或 TCP 重连仍失败：节流告警（防刷屏）
        warn_throttled(f"[tx] not connected/dropped proto={config.TX_PROTO} layout={config.TX_LAYOUT} state={status_byte} gap={gap}(s={gap_s_dbg}) l2s={l2s}(s={l2s_s_dbg}) -> {config.TX_ADDR}")

    return tx_sock


# ---------- 系统状态枚举 ----------
# 这些常量会通过 send_detect_packet 发给上位机，也会被主状态机内部用于分支判断
STATUS_NO_TUBE = 0          # 未检测到瓶子
STATUS_INIT = 1             # 初始化阶段
STATUS_DETECTING = 2        # 检测进行中
STATUS_COMPLETED = 3        # 注液完成
STATUS_PAUSED = 4           # 暂停中
STATUS_STOPPED = 5          # 任务结束
STATUS_RECHECK_FAIL = 7     # 定容复核失败
# 注：6 号刻意留空（可能是上位机协议里被保留或历史代码已弃用），不要复用


STATUS_NAMES = {  # 状态码→可读名称
    STATUS_NO_TUBE: "no_tube",
    STATUS_INIT: "init",
    STATUS_DETECTING: "detecting",
    STATUS_COMPLETED: "completed",
    STATUS_PAUSED: "paused",
    STATUS_STOPPED: "stopped",
    STATUS_RECHECK_FAIL: "recheck_fail",
}


