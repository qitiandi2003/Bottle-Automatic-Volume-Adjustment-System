# 历史资料

这里保留旧版本说明和调试入口，便于追溯代码演变。文中旧目录、设备环境、类名和性能记录可能已失效；当前安装、启动和配置请看 [项目 README](../../README.md) 与 [配置说明](../CONFIGURATION.md)。

| 文件 | 用途 |
| --- | --- |
| [新人入门指南](新人入门指南.md) | 旧版阅读顺序与设计背景 |
| [项目全景](项目全景.md) | 旧模块结构及流程说明 |
| [说明文档](说明文档.md) | 原始补充说明 |
| `scripts/run_stream_test.py`、`.sh` | 旧 Detector 流测试，尚未适配当前包入口 |

旧流测试脚本供参考，不作为启动或自动化测试入口。当前应用安装后使用 `bvc-run`；相机检查使用 `python scripts/probe_camera.py`，无硬件测试使用 `python -m unittest discover -s tests/unit -v`，均从项目根目录操作。

归档来源与包目录迁移说明见 [整合记录](../INTEGRATION.md)。本目录不保存设备登录凭据；部署环境由使用者自行配置。
