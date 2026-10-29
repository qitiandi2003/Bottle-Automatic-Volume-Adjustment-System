# Bottle Automatic Volume Adjustment System

基于海康工业相机的容量瓶自动定容视觉程序：识别瓶身与刻度线、跟踪液面，通过 TCP 与上位机交换控制命令及检测结果。主要部署目标为 Linux / ARM64 的 RDK X5，推荐入口为 `bvc-run`。

源码采用标准 `src/bottle_volume_control/` 包结构。本项目以完整的 v2 外层源码为基础，保留新版检测流程，补回相机格式兼容路径，修复本地录像和记录保留逻辑。来源及验证证据见 [整合说明](docs/INTEGRATION.md)。

## 运行条件

- **Python 3.10 或更新版本**，建议使用独立的 venv。
- 海康工业相机及与系统、CPU 架构匹配的 **MVS / Machine Vision SDK 原生运行库**。包内 `camera/sdk/` 是 Python 封装，仍需单独安装原生 SDK。
- Python 依赖：NumPy、OpenCV、Matplotlib，由 [pyproject.toml](pyproject.toml) 声明。
- 系统命令 `ffmpeg`，包含 `libx264` 编码器，用于会话结束后的 MP4 转码。
- 上位机 TCP 服务，默认地址 `127.0.0.1:9001`；本程序作为客户端连接。

Linux / ARM64 是主要部署环境。Windows 调试需要可加载的 `MvCameraControl.dll`，本次未进行 Windows 相机实测。

## 安装

以下示例适用于 Debian / Ubuntu 系 Linux。先按厂商说明安装海康 SDK，并用厂商工具确认相机可枚举、可取图。

```bash
git clone https://github.com/qitiandi2003/Bottle-Automatic-Volume-Adjustment-System.git
cd Bottle-Automatic-Volume-Adjustment-System

sudo apt-get update
sudo apt-get install python3-venv ffmpeg

python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

ffmpeg -hide_banner -encoders | grep libx264
```

`pip install -e .` 安装项目、依赖及命令入口，修改 `src/` 下源码后无需重新复制文件。`requirements.txt` 转交同一安装方式，旧 `requirments.txt` 也保留兼容转发；依赖以 `pyproject.toml` 为准。

ARM64 上请使用与 Python、系统及架构匹配的软件包，离线安装时在相同环境准备依赖。当前尚未提供经过目标硬件验证的版本锁定文件；部署验收后可保存该设备的 `pip freeze` 结果。

### 配置海康 SDK

`MVCAM_COMMON_RUNENV` 指向包含架构子目录的 SDK 库目录。以安装在 `/opt/MVS` 的 ARM64 设备为例：

```bash
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/aarch64:${LD_LIBRARY_PATH:-}"
test -f "$MVCAM_COMMON_RUNENV/aarch64/libMvCameraControl.so"
```

按实际安装位置调整。ARM64 加载 `aarch64/libMvCameraControl.so`，Linux x86_64 的 64 位 Python 则加载 `64/libMvCameraControl.so`。系统服务和非交互式 SSH 会话也需设置这些环境变量。

GigE 相机与目标机网卡应可互通。主程序、取图脚本和厂商工具通常不能同时独占同一相机。

## 启动与配置

每次新开终端，先进入项目根目录并激活环境，再配置 SDK 和上位机地址。下面的 `192.0.2.10` 是示例，需要替换：

```bash
source .venv/bin/activate
export MVCAM_COMMON_RUNENV=/opt/MVS/lib
export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/aarch64:${LD_LIBRARY_PATH:-}"
export TX_HOST=192.0.2.10
export TX_PORT=9001
export SHOW_WINDOWS=0
bvc-run
```

安装后也可使用 `python -m bottle_volume_control`；项目根目录的 `python main.py` 保留兼容入口，三者运行同一应用。`bvc-run --help` 显示帮助，不加载相机 SDK。

程序打开相机并等待上位机命令。收到 `START` 后建立会话，依次搜索瓶身、锁定刻度并检测液面；上位机应持续提供当前瓶高、刻度高度等数据。按 **Ctrl+C** 正常退出，并为录像收尾留出时间。

`TX_ENABLED=0` 可关闭通信，用于独立相机调试；此时主程序不会自行产生 `START`，完整定容流程仍需控制命令。运行参数主要从 [config.py](src/bottle_volume_control/config.py) 查看，常用环境变量及协议见 [配置说明](docs/CONFIGURATION.md)。

### 运行数据目录

默认数据根目录是**启动工作目录下的 `var/`**：`var/logs/` 放日志和会话录像，`var/images/` 放采集图片。源码目录不存运行产物。

可在启动前设置绝对路径，例如 `export BVC_DATA_DIR=/data/bottle-volume-control`，之后使用该目录下的 `logs/` 和 `images/`。系统服务建议显式设置此变量，使数据位置不依赖启动目录。

### 本地录像与可选 HTTP

**默认记录本地会话图像，HTTP 服务默认关闭。** 本地录像不依赖浏览器访问，也不要求开启 HTTP。

- 文本日志：`var/logs/log_YYYYMMDD.txt`。
- 会话帧：`var/logs/run_<时间戳>/<流名>/frame_NNNNNN.jpg`。
- 多帧流在会话结束后转为同一会话目录下的 `<流名>.mp4`，成功后删除对应 JPEG；单帧流保留为图片。转码失败保留原始图片及错误信息。
- 默认最多保留 20 次会话、7 天、总大小目标 500 MB；当前和待收尾会话暂不参与清理。保留策略在 `app.py` 的 `VizRecorder(...)` 中设置。

上述路径随 `BVC_DATA_DIR` 一起变化。如需浏览图像和日志：

```bash
DEBUG_STREAM_ENABLED=1 DEBUG_STREAM_HOST=127.0.0.1 bvc-run
```

访问 `http://127.0.0.1:8080/`，有图像推送后可看到对应流；`/logs` 查看记录，`/health` 查看服务状态。远程访问时设置目标机监听地址或 `0.0.0.0`，再打开 `http://<目标机IP>:8080/`。服务没有登录认证，应只开放给可信调试网络。图像流活跃时批量日志下载可能返回 503，等会话结束后重试。

默认编码宽度上限 640、JPEG 质量 75，同时影响录像。MP4 按 5 FPS 编码，表示播放帧率，不能用于衡量实际检测速度或真实时间间隔。

## 工具与测试

先停止主程序，再探测相机或采集一张图片：

```bash
python scripts/probe_camera.py
bvc-capture
```

`bvc-capture` 默认保存到数据目录的 `images/`，也可用 `--output <路径>` 指定文件。`python scripts/capture_image.py` 是相同功能的脚本入口。这些命令会真实操作相机。

整理已有采集图片可运行 `bvc-collect --dest-root /absolute/path/to/collected-data`，源目录默认是数据目录的 `images/`，输出自动编号的 `data_001/`。也可使用 `python scripts/collect_debug_data.py --dest-root <目录>`；整理工具不启动相机。

安装项目后，从根目录运行无硬件测试：

```bash
python -m unittest discover -s tests/unit -v
```

测试使用模拟 SDK、合成图片和临时文件。本次目录整理后 40 项测试通过；实际相机和目标机转码仍需验收，见 [验证限制](docs/INTEGRATION.md#验证与限制)。[CI 配置](.github/workflows/tests.yml) 使用 Python 3.10 / 3.12 执行编译与测试，当前改动的远程状态以 Actions 实际结果为准。

[tests/manual/](tests/manual/README.md) 保留手工联调脚本及 notebook，可能连接网络或读取现场样本，请单独检查后运行。旧说明和旧入口位于 [docs/legacy/](docs/legacy/README.md)，不作为当前启动指南。

可选 X5 运维脚本在 `scripts/deploy/`，须自行配置主机、用户、项目目录和 SSH 密钥，详见 [配置说明](docs/CONFIGURATION.md#可选-x5-运维脚本)。

## 项目目录

```text
.
├── pyproject.toml                # 安装元数据、依赖和 CLI
├── main.py                       # 兼容启动入口
├── src/bottle_volume_control/
│   ├── app.py                    # 应用装配与启动
│   ├── config.py                 # 运行参数主入口
│   ├── paths.py                  # 数据、日志、图片目录
│   ├── utils.py                  # 通用及协议打包工具
│   ├── camera/                   # 取图、曝光、像素转换与 SDK 封装
│   ├── vision/                   # 瓶身、刻度线、液面和稳定性算法
│   ├── control/                  # 上位机连接和命令解析
│   ├── workflow/                 # 状态机、阶段处理和主循环
│   └── diagnostics/              # 日志、录像、HTTP 和数据整理
├── scripts/                      # 采集、探测、调色入口
│   └── deploy/                   # 可选 X5 运维脚本
├── tests/unit/                   # 无硬件自动化测试
├── tests/manual/                 # 手工联调与历史样例
├── docs/                         # 配置、整合记录、待验证项
│   └── legacy/                   # 历史说明和旧入口
└── var/                          # 默认运行产物，不提交 Git
    ├── logs/
    └── images/
```

## 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 找不到包或命令 | 是否激活正确 venv，并在项目根目录执行 `pip install -e .` |
| 找不到 SDK / 相机打开失败 | SDK 架构和库路径、相机网络、厂商工具是否可取图、是否被其他程序占用 |
| 始终等待或无上位机连接 | 地址和端口是否正确，接收端是否启动并发送 `START` |
| 找不到录像或图片 | `BVC_DATA_DIR` 与启动工作目录、是否产生会话帧 |
| 只有 JPEG，没有 MP4 | 会话是否结束、`ffmpeg` 是否支持 `libx264`、是否有转码错误 |
| HTTP 无法访问 | 是否开启开关、监听地址与端口是否正确、网络是否允许访问 |
| 定容结果不稳定 | 留存瓶型、光照、曝光和画面，再进行现场标定及复核 |

本次验证不替代真实相机、RDK X5 长时间运行、上位机联动或定容精度验收。
