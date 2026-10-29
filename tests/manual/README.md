# 手工联调与历史样例

这里存放旧联调工具、图片算法样例和 notebook，保留作现场排查参考。它们不属于无硬件自动化测试集；部分脚本运行或导入时会连接网络、读取外部图片或创建窗口，使用前请检查代码中的输入和副作用。

| 文件 | 用途与前提 |
| --- | --- |
| `fake_start.py` | 模拟上位机控制帧，涉及 TCP 监听和连接；须按实际环境核对地址和参数 |
| `test_tcp_cmd.py` | 协议手工联调，需要对应网络端点 |
| `test_liqure.py` | 图片算法试验，需要自行提供样本并检查图形显示环境 |
| `test_logger.ipynb` | 历史日志实验 notebook，单独检查后运行 |

先从项目根目录执行 `pip install -e .`，再按脚本要求准备测试环境。这里的脚本名称保留历史拼写，不表示它们适合通过通用 `unittest` 或 `pytest` 自动发现运行。

正式无硬件检查入口始终为：

```bash
python -m unittest discover -s tests/unit -v
```

实际应用使用 `bvc-run`，图片采集使用 `bvc-capture`。当前目录说明与运行配置见 [README](../../README.md) 和 [配置说明](../../docs/CONFIGURATION.md)。
