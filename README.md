# LLM Analysis

模型结构、KV Cache、显存容量和纯 Prefill / Decode 性能分析网页。

在线页面：<https://dragondream-chen.github.io/LLM-Analysis/?model=deepseek-v4.1-flash#operators>

## 本机 msmodeling

DeepSeek 的第四页提供独立的本地 TensorCast 面板，可执行固定 batch 的 P/D 仿真，
显示原生耗时、显存、吞吐与算子分解，并导出日志与时间线。

安装、运行方式和当前口径见 [本机 msmodeling 接入指南](docs/msmodeling-integration.md)。

```powershell
python scripts/serve-msmodeling.py
```

启动后打开 <http://127.0.0.1:8765/?model=deepseek-v4.1-flash#performance>。
GitHub Pages 托管静态页面，本机服务执行 Python 仿真。

原网页的解析估计和原生 msmodeling 结果分别展示。所有仿真值均需通过目标环境实测校准，
当前接入不代表 CED Prefill 优化路径或真实服务吞吐已经验收。

上游网页：<https://github.com/Biuapha/LLM-Analysis>

仿真引擎：<https://gitcode.com/Ascend/msmodeling>
