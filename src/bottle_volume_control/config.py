# -*- coding: utf-8 -*-
# =============================================================================
# config.py —— 运行时全局配置（集中管理所有可调参数）
# -----------------------------------------------------------------------------
# 本模块定义可由环境变量覆盖的运行参数。SDK 封装通过 camera.sdk 包导入，
# 不修改全局模块搜索路径，也不在配置加载时打开相机。
# -----------------------------------------------------------------------------
# 设计原则：
#   - 所有 bool 型开关用 rt_util._env_flag(name, default) 读取，兼容 0/false/no/off；
#   - 数值型用 try/except + float()/int()，遇到非法值静默回退到安全默认；
#   - 涉及像素阈值的常量在文件里都带"单位(px)"注释，直接看就懂。
# =============================================================================
from . import utils as rt_util
from .paths import images_dir
import sys
import os

# ---------------- 启动曝光（boot exposure） ----------------
# EXP_BOOT_ENABLED : 相机打开后是否立即把曝光强行设为 EXP_BOOT_US
# EXP_BOOT_US      : 启动曝光的初始值，默认 14 ms
EXP_BOOT_ENABLED = rt_util._env_flag('EXP_BOOT_ENABLED', default=True)
try:
    EXP_BOOT_US = float(os.getenv('EXP_BOOT_US', '14000'))
except Exception:
    EXP_BOOT_US = 14000.0           # 非数字值 → 回退到默认

LIQ_DEBUG_SILENT = True              # 液面检测模块内部日志总开关；True=静默

# ---------------- 是否显示可视化窗口 ----------------
# Windows 默认有图形界面，其他平台根据 DISPLAY/WAYLAND 判断
# 这里逻辑：平台 Windows 或存在 DISPLAY/WAYLAND_DISPLAY 环境变量 → 有显示
_has_display = bool(
    (sys.platform == 'win32')
    or os.getenv('DISPLAY')
    or os.getenv('WAYLAND_DISPLAY')
)

# 最终 SHOW_WINDOWS 可以被 SHOW_WINDOWS env 覆盖；默认跟随 _has_display
SHOW_WINDOWS = rt_util._env_flag('SHOW_WINDOWS', default=_has_display)


# ---------------- TX (TCP/UDP) ----------------
# 参考 GrabImage_Cv.py：支持 tcp/udp，两种布局；此处默认 tcp + cpp 布局
# 允许通过环境变量关闭传输以避免在无接收端时反复连接报错：TX_ENABLED=0
TX_ENABLED = rt_util._env_flag('TX_ENABLED', default=True)  # 是否启用发送（无接收端调试时可关闭）
TX_PROTO = os.getenv('TX_PROTO', 'tcp').lower()  # 传输协议: 'tcp' 或 'udp'
TX_LAYOUT = os.getenv('TX_LAYOUT', 'cpp').lower()  # 数据布局: 'std' 或 'cpp'
# 目标地址是 (host, port) 元组；int(...) 保证端口是整数
TX_ADDR = (os.getenv('TX_HOST', '127.0.0.1'), int(os.getenv('TX_PORT', '9001')))  # 接收端地址
TX_VERBOSE = rt_util._env_flag('TX_VERBOSE', default=False)  # 是否打印完整发送日志

# 复核失败时的额外调试开关
RECHECK_DEBUG = rt_util._env_flag('RECHECK_DEBUG', default=False)

# ---------------- Phase 级 verbose (新框架 phases/*) ----------------
# 默认 OFF 防止刷屏; 排查具体相位时按需打开. 走 Asynclogger 进 logs/log_YYYYMMDD_HHMM.txt 统一管理.
# LOCK_SCALE_VERBOSE : LockScalePhase 每帧打 (刻度 x / dx / score / base / 阈值 / conf, gap_stab 稳定时长 + 样本数)
# DOSING_VERBOSE     : DosingPhase 每帧打 (gap / l2s / raw / pinned / peak / gate / peaks_lmr)
LOCK_SCALE_VERBOSE = rt_util._env_flag('LOCK_SCALE_VERBOSE', default=False)
DOSING_VERBOSE     = rt_util._env_flag('DOSING_VERBOSE',     default=False)


_TX_RETRY_INTERVAL = 5.0  # TCP 断线重连间隔(秒)


# ---------------- 自动曝光 ----------------
# 仅"首次检测到试管"时做一次自动曝光，之后不再自动变动 —— 避免中途闪烁影响液面判断
AUTO_EXPO_ONCE = rt_util._env_flag('AUTO_EXPO_ONCE', default=True)  # 仅首次检测到试管时自动调光

# 刻度细化参数（默认开启；band≈8-10、平滑核≈7-11，禁用掩膜加权）
# SCALE_REFINE_ENABLED : 是否对刻度位置做"亚像素细化"
# SCALE_REFINE_BAND    : 细化时在 x 方向左右各看多少列（clamp 到 [8, 10]）
# SCALE_REFINE_SMOOTH  : 细化平滑核半径（clamp 到 [7, 11]，且强制奇数）
# SCALE_REFINE_USE_MASK: 是否用试管掩膜加权
# SCALE_REFINE_DEBUG   : 细化调试开关
SCALE_REFINE_ENABLED = rt_util._env_flag('SCALE_REFINE_ENABLED', default=True)  # 是否启用刻度细化
try:
    SCALE_REFINE_BAND = int(os.getenv('SCALE_REFINE_BAND', '9'))  # 刻度细化横向带宽
except Exception:
    SCALE_REFINE_BAND = 10
# 再做一次 clamp：max(8, min(10, x)) 把值限制到 [8,10]
SCALE_REFINE_BAND = max(8, min(10, SCALE_REFINE_BAND))
try:
    SCALE_REFINE_SMOOTH = int(os.getenv('SCALE_REFINE_SMOOTH', '9'))  # 细化平滑核半径
except Exception:
    SCALE_REFINE_SMOOTH = 9
SCALE_REFINE_SMOOTH = max(7, min(11, SCALE_REFINE_SMOOTH))
# 强制奇数：若偶数则+1（除非已到上限 11，此时-1）；高斯核要求奇数尺寸
if (SCALE_REFINE_SMOOTH % 2) == 0:
    SCALE_REFINE_SMOOTH = SCALE_REFINE_SMOOTH + 1 if SCALE_REFINE_SMOOTH < 11 else SCALE_REFINE_SMOOTH - 1
SCALE_REFINE_USE_MASK = rt_util._env_flag('SCALE_REFINE_USE_MASK', default=False)  # 是否使用掩膜加权
SCALE_REFINE_DEBUG = rt_util._env_flag('SCALE_REFINE_DEBUG', default=False)  # 是否展示细化调试信息

# ---------------- 液面越界判定阈值与最终快照配置 ----------------
# 下面这一组阈值配合"状态机"一起工作：
#   L2S_JUMP_*        判断液面距离是否出现"跨阈值跳变"
#   L2S_SPIKE_*       防止瞬时尖峰被当成合法液面
#   LIQ_NEAR_*        液面"接近刻度"的连续帧判定
#   LIQ_JUMP_CONFIRM_*跳变后要连续高值多少帧才确认
#   LIQ_WARMUP_DETS   刚启用液面检测后的保护期（忽略前 N 次判定）
#   LIQ_FAR_*         远距解锁相关
try:
    L2S_JUMP_LOW = float(os.getenv('L2S_JUMP_LOW', '36'))  # 液面跳变低阈值(px)
except Exception:
    L2S_JUMP_LOW = 36.0
try:
    L2S_JUMP_HIGH = float(os.getenv('L2S_JUMP_HIGH', '100'))  # 液面跳变高阈值(px)
except Exception:
    L2S_JUMP_HIGH = 100.0
try:
    L2S_SPIKE_LOW_MAX = float(os.getenv('L2S_SPIKE_LOW_MAX', '500'))  # 跳变过滤：小值上限(px)
except Exception:
    L2S_SPIKE_LOW_MAX = 500.0
try:
    L2S_SPIKE_HIGH_MIN = float(os.getenv('L2S_SPIKE_HIGH_MIN', '700'))  # 跳变过滤：高值起点(px)
except Exception:
    L2S_SPIKE_HIGH_MIN = 700.0
try:
    L2S_SPIKE_STREAK = int(os.getenv('L2S_SPIKE_STREAK', '3'))  # 触发过滤的连续小值帧数
except Exception:
    L2S_SPIKE_STREAK = 3
L2S_SPIKE_STREAK = max(1, L2S_SPIKE_STREAK)         # 至少 1 帧
try:
    LIQ_NEAR_RADIUS = float(os.getenv('LIQ_NEAR_RADIUS', '200'))  # 进入液面确认的水平距离(px)
except Exception:
    LIQ_NEAR_RADIUS = 200.0
try:
    LIQ_NEAR_FRAMES = int(os.getenv('LIQ_NEAR_FRAMES', '2'))  # 需要连续满足的处理帧数
except Exception:
    # 注：环境变量未设时默认 2，但异常时回落到 7 —— 两者不一致看起来像历史代码的遗留
    LIQ_NEAR_FRAMES = 7
LIQ_NEAR_FRAMES = max(0, LIQ_NEAR_FRAMES)
try:
    LIQ_JUMP_CONFIRM_FRAMES = int(os.getenv('LIQ_JUMP_CONFIRM_FRAMES', '1'))  # 跳变后需连续高值帧数
except Exception:
    LIQ_JUMP_CONFIRM_FRAMES = 3
LIQ_JUMP_CONFIRM_FRAMES = max(1, LIQ_JUMP_CONFIRM_FRAMES)
try:
    LIQ_JUMP_CONFIRM_RADIUS = float(os.getenv('LIQ_JUMP_CONFIRM_RADIUS', '300'))  # 连续高值最小距离(px)
except Exception:
    LIQ_JUMP_CONFIRM_RADIUS = 300.0
try:
    LIQ_WARMUP_DETS = int(os.getenv('LIQ_WARMUP_DETS', '3'))  # 启用液面检测后的保护期(检测次数)
except Exception:
    LIQ_WARMUP_DETS = 10
LIQ_WARMUP_DETS = max(0, LIQ_WARMUP_DETS)
try:
    LIQ_FAR_MIN = float(os.getenv('LIQ_FAR_MIN', '200'))  # 远距解锁的最小距离(px)
except Exception:
    LIQ_FAR_MIN = 200.0
try:
    LIQ_FAR_NEED = int(os.getenv('LIQ_FAR_NEED', '2'))  # 远距解锁需要的连续帧数
except Exception:
    LIQ_FAR_NEED = 5
LIQ_FAR_NEED = max(1, LIQ_FAR_NEED)

# 液面到位后的最终快照路径，位于统一的运行数据目录。
# 上位机可以通过读这个文件确认"最终到位时"的瓶身图像
FINAL_SNAPSHOT_PATH = os.getenv(
    'LIQ_FINAL_SNAPSHOT',
    str(images_dir() / 'liq_final.png')
)

# 过程图保存模式：
#   raw - 保存原始相机图
#   viz - 保存当前阶段可视化结果
#   none - 不保存过程图（默认）
# 注：下面判断里默认值是 'raw'，但注释里写的是 'none'；以代码为准，实际默认是 raw
PROCESS_FRAME_MODE = str(os.getenv('PROCESS_FRAME_MODE', 'raw')).strip().lower()
# 非白名单值统一回落到 'none'
if PROCESS_FRAME_MODE not in ('viz', 'raw', 'none'):
    PROCESS_FRAME_MODE = 'none'
