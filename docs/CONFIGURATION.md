# 配置说明

运行参数主入口是 [config.py](../src/bottle_volume_control/config.py)。环境变量在模块导入时读取，请在启动 `bvc-run` 或 `python -m bottle_volume_control` 前设置。布尔值建议写 `1` / `0`，数值参数应提供合法数字。

## 数据目录

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `BVC_DATA_DIR` | 启动工作目录下的 `var/` | 统一运行数据根目录，系统服务建议设置绝对路径 |

路径由包内 `paths.py` 提供。日志和录像写入 `<数据目录>/logs/`，采集图片写入 `<数据目录>/images/`，目录在需要时创建。安装目录与数据目录相互独立。

例如 `BVC_DATA_DIR=/data/bottle-volume-control bvc-run`。`bvc-capture`、`bvc-collect` 使用相同规则；显式 `--output` / `--src` 可选择其他位置。

## SDK 与显示

| 变量 | 默认值 / 行为 | 用途 |
| --- | --- | --- |
| `MVCAM_COMMON_RUNENV` | 由环境提供 | SDK 库目录；ARM64 加载其 `aarch64/libMvCameraControl.so`，Linux x86_64 的 64 位 Python 加载其 `64/libMvCameraControl.so` |
| `LD_LIBRARY_PATH` | 系统环境 | Linux 共享库和依赖搜索路径 |
| `SHOW_WINDOWS` | Windows 或存在 `DISPLAY` / `WAYLAND_DISPLAY` 时为真 | 无桌面环境建议 `0`；独立调试脚本可能自行创建窗口 |
| `EXP_BOOT_ENABLED` | `1` | 打开相机后设置启动曝光 |
| `EXP_BOOT_US` | `14000` | 启动曝光，单位微秒 |
| `AUTO_EXPO_ONCE` | `1` | 首次检测到瓶身时执行一次自动曝光 |

`camera/sdk/` 是海康 Python 封装，不能替代原生 SDK。常见 8 位 Mono / RGB / BGR / Bayer 图像通过 NumPy / OpenCV 转换，其他格式回退 SDK 转换，HB 数据依赖 SDK 解压。底层彩色输出为 RGB，框架相机适配器转换为算法使用的 BGR。

## 上位机通信

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TX_ENABLED` | `1` | 是否启用通信；`0` 时不接收控制命令 |
| `TX_HOST` | `127.0.0.1` | 上位机服务地址 |
| `TX_PORT` | `9001` | 上位机服务端口 |
| `TX_PROTO` | `tcp` | 结果发送协议，完整双向联调使用 `tcp` |
| `TX_LAYOUT` | `cpp` | `cpp` 为 8 字节结果布局，`std` 为 7 字节，需与上位机一致 |
| `TX_VERBOSE` | `0` | 输出通信调试信息 |

程序为 TCP 客户端，同一连接接收命令并发送结果。命令由 [control/channel.py](../src/bottle_volume_control/control/channel.py) 解析：

```text
AA | cmd(u8) | height(f32 BE) | scale_height(f32 BE) | volume(f32 BE) | 55
```

命令帧长 15 字节，`cmd` 为 `0` 心跳、`1` 开始、`2` 暂停、`3` 停止。`height`、`scale_height` 参与流程判断；`volume` 被接收保存，当前流程未据此自动计算目标体积。结果包包含状态、刻度偏移和液面距离，打包与无效值见 [utils.py](../src/bottle_volume_control/utils.py)。

`TX_PROTO=udp` 仅涉及结果发送，控制接入仍围绕 TCP 连接构建，不能视为已验证的 UDP 双向控制模式。

## 录像、HTTP 与日志

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DEBUG_STREAM_ENABLED` | `0` | 是否启动 HTTP 服务，本地录像独立工作 |
| `DEBUG_STREAM_HOST` | `0.0.0.0` | HTTP 监听地址；仅本机调试可设 `127.0.0.1` |
| `DEBUG_STREAM_PORT` | `8080` | HTTP 端口 |
| `DEBUG_STREAM_MAX_WIDTH` | `640` | JPEG 宽度上限，同时影响录像 |
| `DEBUG_STREAM_QUALITY` | `75` | JPEG 质量，范围 1–100 |
| `PIPELINE_TIMING` | `0` | `1` 时记录各阶段 handler 耗时 |
| `LOCK_SCALE_VERBOSE` | `0` | 刻度锁定诊断日志 |
| `DOSING_VERBOSE` | `0` | 液面阶段诊断日志 |
| `RECHECK_DEBUG` | `0` | 复核失败诊断 |

本地记录随主程序启用，目前没有单独的录像关闭环境变量。每次 `START` 建立会话，关闭后转码；正常退出也请求收尾。

`app.py` 的 `VizRecorder(...)` 设置最近 20 次、7 天、500 MB 总大小目标和 MP4 帧率 5。当前 / 待转码 / 正在转码会话受保护，可能暂时超过目标，收尾后再清理。记录和文本日志统一写入数据目录的 `logs/`，文本日志默认保留 7 天。

HTTP 路由：

- `/`：图像流索引；`/stream/<流名>`：MJPEG；`/snapshot/<流名>`：最近一张 JPEG。
- `/logs`：日志和会话文件索引；`/logs/download`：批量下载，图像活跃期间拒绝该请求。
- `/health`：服务健康检查。

HTTP 无登录认证，访问者可查看图像及记录，仅在可信网络开启。监听 `127.0.0.1` 可以限制为本机访问。

## 算法参数

`config.py` 中的 `SCALE_REFINE_*`、`L2S_JUMP_*`、`LIQ_NEAR_*` 等控制刻度细化、液面跳变和连续帧确认。阈值与瓶型、画面尺寸、曝光及光照有关，修改后需现场复核。部分阈值仍直接定义在 `workflow/` 和 `vision/` 模块中。

`PROCESS_FRAME_MODE`、`LIQ_FINAL_SNAPSHOT` 为保留配置，当前框架未读取它们来控制录像或最终快照；当前记录由 `diagnostics/recorder.py` 处理。

## 可选 X5 运维脚本

`scripts/deploy/x5*.sh` 使用自行配置的目标和 SSH 密钥，不保存设备密码：

```bash
export X5_HOST=your-device-host
export X5_USER=your-device-user
export X5_PROJECT_DIR=/absolute/path/to/Bottle-Automatic-Volume-Adjustment-System
export X5_SSH_PORT=22
export X5_SSH_KEY="$HOME/.ssh/id_ed25519"
```

主机、用户和目录替换为实际值。脚本使用公钥认证并严格检查主机密钥，须先核实并加入 `known_hosts`；未设置 `X5_SSH_KEY` 时使用 SSH agent 或标准密钥。各脚本参数以脚本内说明为准，运行前核对复制和补丁目标。历史相机脚本需要真实硬件，尚未进行本次实机验证。
