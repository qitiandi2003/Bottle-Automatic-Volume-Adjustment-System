# 配置说明

环境变量在模块导入时读取，请在启动 `python main.py` 前设置。以下默认值来自当前源码。布尔值建议写 `1` 或 `0`，数值参数应提供合法数字。

## SDK 与显示

| 变量 | 默认值 / 行为 | 用途 |
| --- | --- | --- |
| `MVCAM_COMMON_RUNENV` | 由环境提供 | 海康 SDK 库目录；ARM64 加载 `aarch64/libMvCameraControl.so`，Linux x86_64 / 64 位 Python 加载 `64/libMvCameraControl.so` |
| `LD_LIBRARY_PATH` | 系统环境 | Linux 共享库及依赖的搜索路径 |
| `SHOW_WINDOWS` | Windows 或存在 `DISPLAY` / `WAYLAND_DISPLAY` 时为真 | 显示配置，无桌面环境建议 `0`；独立调试脚本仍可能自行创建窗口 |
| `EXP_BOOT_ENABLED` | `1` | 打开相机后设置启动曝光 |
| `EXP_BOOT_US` | `14000` | 启动曝光，单位微秒 |
| `AUTO_EXPO_ONCE` | `1` | 首次检测到瓶身时执行一次自动曝光 |

`MvImport/` 不能替代原生 SDK。常见 8 位 Mono / RGB / BGR / Bayer 图像通过 NumPy / OpenCV 转换，其他格式回退 SDK 转换，HB 数据依赖 SDK 解压。底层彩色输出保持 RGB，框架相机适配器转换为算法使用的 BGR。

## 上位机通信

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `TX_ENABLED` | `1` | 是否启用通信；`0` 时不接收控制命令 |
| `TX_HOST` | `127.0.0.1` | 上位机服务地址 |
| `TX_PORT` | `9001` | 上位机服务端口 |
| `TX_PROTO` | `tcp` | 结果发送协议；完整双向控制联调使用 `tcp` |
| `TX_LAYOUT` | `cpp` | `cpp` 为 8 字节结果布局，`std` 为 7 字节布局，需与上位机一致 |
| `TX_VERBOSE` | `0` | 输出通信调试信息 |

程序为 TCP 客户端，同一连接接收命令并发送结果。命令由 [control_channel.py](../control_channel.py) 解析：

```text
AA | cmd(u8) | height(f32 BE) | scale_height(f32 BE) | volume(f32 BE) | 55
```

命令帧长 15 字节，`cmd` 为 `0` 心跳、`1` 开始、`2` 暂停、`3` 停止。`height` 和 `scale_height` 参与流程判断；`volume` 被接收保存，当前流程未据此自动计算目标体积。结果包包含状态、刻度偏移和液面距离，打包与无效值见 [rt_util.py](../rt_util.py)。

`TX_PROTO=udp` 仅涉及结果发送，当前接入仍围绕 TCP 控制连接构建，不能视为已验证的 UDP 双向控制模式。

## 录像、HTTP 与日志

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DEBUG_STREAM_ENABLED` | `0` | 是否启动 HTTP 服务；本地录像独立工作 |
| `DEBUG_STREAM_HOST` | `0.0.0.0` | HTTP 监听地址；仅本机调试可设 `127.0.0.1` |
| `DEBUG_STREAM_PORT` | `8080` | HTTP 端口 |
| `DEBUG_STREAM_MAX_WIDTH` | `640` | JPEG 宽度上限，同时影响本地录像 |
| `DEBUG_STREAM_QUALITY` | `75` | JPEG 质量，范围 1–100 |
| `PIPELINE_TIMING` | `0` | `1` 时记录各阶段 handler 耗时 |
| `LOCK_SCALE_VERBOSE` | `0` | 刻度锁定诊断日志 |
| `DOSING_VERBOSE` | `0` | 液面阶段诊断日志 |
| `RECHECK_DEBUG` | `0` | 复核失败相关诊断 |

本地记录随主程序启用，当前没有单独的录像关闭环境变量。每次 `START` 建立会话，关闭会话后转码；正常退出也会请求收尾。

`main.py` 的 `VizRecorder(...)` 设置最近 20 次、7 天、500 MB 总大小目标及 MP4 编码帧率 5。当前 / 待转码 / 正在转码会话暂时受保护，可能使数量或容量超过目标，收尾后再清理。记录目录是相对启动工作目录的 `logs/`，应从项目根目录启动。文本日志写入项目根目录 `logs/`，默认保留 7 天。

HTTP 路由：

- `/`：图像流索引；`/stream/<流名>`：MJPEG；`/snapshot/<流名>`：最近一张 JPEG。
- `/logs`：日志和会话文件索引；`/logs/download`：批量下载，图像活跃期间会拒绝该请求。
- `/health`：服务健康检查。

HTTP 无登录认证，访问者可查看图像及记录。仅在可信网络开启；设为 `127.0.0.1` 可限制本机访问。

## 算法参数

`rt_config.py` 包含 `SCALE_REFINE_*`、`L2S_JUMP_*`、`LIQ_NEAR_*` 等刻度细化、液面跳变和连续帧确认参数。阈值与瓶型、画面尺寸、曝光和光照有关，修改后需现场复核。部分阈值直接定义在状态机、阶段处理和算法模块中。

`PROCESS_FRAME_MODE`、`LIQ_FINAL_SNAPSHOT` 是保留配置，当前框架未读取它们来控制录像或最终快照；当前记录由 `VizRecorder` 处理。

## 可选 X5 运维脚本

`docs/scripts/x5*.sh` 使用自行配置的目标与 SSH 密钥，不提供设备登录密码。常用配置：

```bash
export X5_HOST=your-device-host
export X5_USER=your-device-user
export X5_PROJECT_DIR=/absolute/path/to/Bottle-Automatic-Volume-Adjustment-System
export X5_SSH_PORT=22
export X5_SSH_KEY="$HOME/.ssh/id_ed25519"
```

主机、用户和项目目录须替换为实际值。脚本使用公钥认证并严格检查主机密钥，须先核实设备主机密钥并加入本机 `known_hosts`；未设置 `X5_SSH_KEY` 时使用 SSH agent 或标准密钥。各脚本参数以脚本内说明为准，运行前检查复制和补丁应用的目标路径。历史相机脚本需要真实硬件，尚未进行本次实机验证。
