# Bottle Automatic Volume Adjustment System

基于海康工业相机的容量瓶自动定容视觉程序：识别瓶身与刻度线、跟踪液面，通过 TCP 与上位机交换控制命令和检测结果。主要部署目标为 Linux / ARM64 的 RDK X5，入口为 `main.py`。

本仓库以完整的 v2 外层源码为基础，保留新版检测流程，补回相机格式兼容路径，修复本地录像与记录保留逻辑。来源、恢复证据和验证范围见 [整合说明](docs/INTEGRATION.md)。

## 运行条件

- **Python 3.10 或更新版本**，建议使用独立的 venv。
- 海康工业相机及与操作系统、CPU 架构匹配的 **MVS / Machine Vision SDK 原生运行库**。仓库内的 `MvImport/` 是 Python 封装，仍需单独安装原生 SDK。
- Linux / ARM64 为主要目标环境；Windows 调试同样需要可加载的 `MvCameraControl.dll`。本次未完成 Windows 相机实测。
- Python 依赖：NumPy、OpenCV、Matplotlib，见 [requirements.txt](requirements.txt)。
- 系统命令 `ffmpeg`，包含 `libx264` 编码器，用于会话结束后的 MP4 转码。
- 上位机 TCP 服务；默认地址 `127.0.0.1:9001`，本程序作为客户端连接。

## 安装

以下示例适用于 Debian / Ubuntu 系 Linux。在目标机上先按厂商说明安装海康 SDK，并用厂商工具确认相机可枚举、可取图。

```bash
git clone https://github.com/qitiandi2003/Bottle-Automatic-Volume-Adjustment-System.git
cd Bottle-Automatic-Volume-Adjustment-System

sudo apt-get update
sudo apt-get install python3-venv ffmpeg

python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

ffmpeg -hide_banner -encoders | grep libx264
```

ARM64 上请使用与 Python 版本、系统和 CPU 架构匹配的软件包。离线安装时，在同架构、同 Python 版本环境准备依赖。依赖文件沿用原项目的三项依赖，尚未提供经过目标硬件验证的版本锁定文件；部署验收后可保存该设备的 `pip freeze` 结果。

旧命令 `pip install -r requirments.txt` 仍兼容，该文件转交给标准拼写的 `requirements.txt`。

### 配置海康 SDK

`MVCAM_COMMON_RUNENV` 指向包含架构子目录的 SDK 库目录。例如 SDK 安装在 `/opt/MVS` 的 ARM64 设备：

```bash
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/aarch64:${LD_LIBRARY_PATH:-}"
test -f "$MVCAM_COMMON_RUNENV/aarch64/libMvCameraControl.so"
```

按实际安装位置调整。当前封装在 ARM64 加载 `aarch64/libMvCameraControl.so`，Linux x86_64 的 64 位 Python 则加载 `64/libMvCameraControl.so`。系统服务与非交互式 SSH 会话也需配置这些环境变量。

GigE 相机与目标机网卡应可互通。主程序、取图脚本和厂商工具通常不能同时独占同一相机。

## 启动与使用

每次新开终端，进入项目根目录并激活环境，再设置 SDK 和上位机地址。下面的 `192.0.2.10` 是示例，需要替换：

```bash
source .venv/bin/activate
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/aarch64:${LD_LIBRARY_PATH:-}"

export TX_HOST=192.0.2.10
export TX_PORT=9001
export SHOW_WINDOWS=0
python main.py
```

程序打开相机并等待上位机命令。收到 `START` 后开启检测会话，进行瓶身搜索、刻度锁定和液面检测；上位机应持续提供当前瓶高、刻度高度等控制数据。暂停、停止和完成状态由控制协议处理。按 **Ctrl+C** 正常退出，并为录像收尾留出时间。

`TX_ENABLED=0` 可关闭上位机通信，适用于独立相机调试；此时主程序不会自行产生 `START`，完整定容流程仍需控制命令。详细参数及协议入口见 [配置说明](docs/CONFIGURATION.md)。

### 本地录像与可选 HTTP 调试

**默认记录本地会话图像，HTTP 服务默认关闭。** 本地录像不依赖浏览器访问，也不要求开启 HTTP。

- 日志：`logs/log_YYYYMMDD.txt`。
- 会话图像：`logs/run_<时间戳>/<流名>/frame_NNNNNN.jpg`。
- 会话结束后，多帧图像流经 `ffmpeg` 转换为 `logs/run_<时间戳>/<流名>.mp4`；成功后删除对应 JPEG。单帧流保留为图片；缺少 `ffmpeg` 或转换失败时保留原始 JPEG 和错误信息。
- 默认保留策略：最多 20 次会话、最长 7 天、总大小目标上限 500 MB；当前会话和待收尾会话暂不参与清理。参数在 `main.py` 的 `VizRecorder(...)` 中设置。

如需浏览图像和日志，可在启动前开启 HTTP：

```bash
DEBUG_STREAM_ENABLED=1 DEBUG_STREAM_HOST=127.0.0.1 python main.py
```

打开 `http://127.0.0.1:8080/`，有图像推送后可看到对应流；`/logs` 查看日志和录像，`/health` 查看服务状态。远程查看时，将 `DEBUG_STREAM_HOST` 改为目标机监听地址或 `0.0.0.0`，再访问 `http://<目标机IP>:8080/`。服务没有登录认证，应仅开放给可信调试网络。批量日志下载在图像流活跃时可能返回 503，可等会话结束后重试。

默认编码宽度上限 640、JPEG 质量 75，同时影响本地录像。MP4 默认按 5 FPS 编码，表示播放帧率，不能用来衡量实际检测速度或真实时间间隔。

## 调试与测试

### 检查相机

先停止主程序，再查看相机参数或保存单张图片：

```bash
python docs/scripts/probe_camera.py
python capture_single_image.py --output single_images/check.png
```

这些命令会真实连接相机。没有相机和 SDK 的环境请使用下面的自动化测试。

### 无硬件自动化测试

在项目根目录安装 Python 依赖后执行：

```bash
python -m unittest discover -s tests/unit -v
```

测试使用模拟 SDK、合成图像和临时文件，不要求真实相机或上位机；模拟转码不能替代目标设备实际编码验收。验证环境与限制见 [整合说明](docs/INTEGRATION.md#验证与限制)。

[GitHub Actions 配置](.github/workflows/tests.yml) 在推送和拉取请求时，使用 Python 3.10 / 3.12 执行语法编译和上述测试；实际运行结果以仓库 Actions 页面为准。

`tests/` 还保留部分历史联调工具，可能连接网络、读取外部图片或操作相机；部分脚本仍引用旧入口。请按用途单独检查，避免对全部旧脚本执行通用测试发现。

`docs/scripts/x5*.sh` 为可选运维辅助脚本，使用前须自行配置目标主机、用户、项目目录和 SSH 密钥，见 [配置说明](docs/CONFIGURATION.md#可选-x5-运维脚本)。

### 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 找不到 SDK 共享库 / DLL | SDK 是否安装、架构是否匹配、环境变量与库搜索路径是否正确 |
| 相机打开失败或被占用 | 厂商工具是否能取图、网络是否互通、是否被另一程序占用 |
| 无上位机连接或一直等待 | `TX_HOST` / `TX_PORT` 是否正确，接收端是否启动并发送 `START` |
| 有日志但无会话录像 | 是否收到 `START` 并产生可视化帧，检查会话目录与运行日志 |
| 只有 JPEG，没有 MP4 | 会话是否结束，`ffmpeg` 是否支持 `libx264`，是否有转码错误文件 |
| HTTP 无法访问 | 是否开启开关、监听地址与端口是否正确、网络是否允许访问 |
| 定容结果不稳定 | 记录瓶型、光照、曝光和原始画面，再进行现场标定与复核 |

## 项目目录

```text
.
├── main.py                     # 主入口
├── rt_config.py                # 环境变量与运行参数
├── camera_io.py                # 海康取图、像素转换与缓冲区管理
├── control_channel.py          # 上位机控制帧解析
├── auto_exposure.py            # 自动曝光及瓶型相关处理
├── tube.py / center_scale.py    # 瓶身与刻度线检测
├── li_det.py                   # 液面检测
├── debug_stream.py             # 图像编码、可选 HTTP 服务
├── logger_utils.py             # 异步日志与清理
├── capture_single_image.py      # 单张取图工具
├── collect_debug_data.py        # 已有数据目录归档工具
├── framework/                  # 检测流程、状态机、通信及录像适配
├── MvImport/                   # 海康 SDK Python 封装
├── tests/unit/                 # 无硬件自动化测试
├── docs/                       # 配置、整合记录及历史说明
├── requirements.txt            # 标准依赖入口
└── logs/                       # 运行时生成
```

历史说明中的旧目录、设备地址及性能数字可能已过时，以当前入口和配置为准。真实相机采集、RDK X5 稳定性、上位机联动和定容精度仍需现场验收。
