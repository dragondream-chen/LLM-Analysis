# 本机 msmodeling 接入

本接入在 DeepSeek-V4.1-Flash 的“纯 P / 纯 D 仿真”页增加原生 TensorCast 面板。
GitHub Pages 继续提供静态网页，本机 Python 服务执行仿真并返回结构化结果。
失败会显示原始错误，不会用网页解析结果替代。

## 本机启动

本项目已验证的上游修订为 `413889b5b7007803fcc140edf2d89eff733c5890`。
需要 Python 3.10+、Git 和 uv；依赖仅安装到 `.local/msmodeling/.venv`。

首次准备（PowerShell）：

```powershell
Set-Location D:\code\LLM-Analysis
$env:GIT_LFS_SKIP_SMUDGE = '1'
git clone --filter=blob:none --no-checkout https://github.com/Ascend/msmodeling.git .local/msmodeling
git -C .local/msmodeling fetch --depth 1 origin 413889b5b7007803fcc140edf2d89eff733c5890
git -C .local/msmodeling checkout --detach 413889b5b7007803fcc140edf2d89eff733c5890
uv sync --project .local/msmodeling --no-dev --frozen --python python
uv pip check --python .local/msmodeling/.venv/Scripts/python.exe
```

跳过的 LFS 内容是本接入不使用的上游资产；本接入只支持 `analytic`，不宣称已安装或命中 Profiling 数据库。
若网络需要代理，在当前终端配置即可；不要修改系统或用户全局配置。
若 uv 下载 Torch 后被 Windows 拒绝重命名缓存目录，可以在同一 `.venv` 中用 pip 安装上游锁定的 CPU Torch 后重新同步。

启动：

```powershell
python scripts/serve-msmodeling.py
```

打开 <http://127.0.0.1:8765/?model=deepseek-v4.1-flash#performance>。
也可在自己的 GitHub Pages 页面第四页连接 `http://127.0.0.1:8765`；浏览器可能要求允许访问本地网络。
同源本地页面不依赖这个跨源权限。
服务仅监听 `127.0.0.1`。按 Ctrl+C 停止，正在运行的任务会被取消和回收。

自定义已有引擎位置：

```powershell
python scripts/serve-msmodeling.py --msmodeling-path D:\tools\msmodeling --python D:\tools\msmodeling\.venv\Scripts\python.exe
```

## 使用

1. 顶部选择 DeepSeek-V4.1-Flash，进入“纯 P / 纯 D 仿真”。
2. 在本地 msmodeling 面板检查连接，设置每 DP 的固定 batch。
3. 在原网页设置输入/输出长度、P/D 硬件与 TP/EP、容量预算和时延目标。
4. 点击运行纯 P 或纯 D。DSpark 的每周期平均提交数单独输入，包含 anchor/bonus；AR 固定为 1。
5. 查看原生耗时、请求/Token 吞吐、显存、Host 容量、算子分解；下载 JSON、日志或 Chrome Trace。

本地 RAM、权重与设备显存是不同指标。`simulatorWallSeconds` 是本机执行仿真程序的耗时，
`latencySeconds` 是目标设备的模型预测耗时，`peakMemoryGiB` 是目标设备上的模型显存估计。
不会加载模型权重或初始化 NPU/CUDA 上下文；输入是随页面保存的固定 checkpoint 配置。

## 首版有效口径

| 项目 | 本地引擎采用的口径 |
| --- | --- |
| 模型 | `deepseek-ai/DeepSeek-V4.1-Flash`，配置修订 `314113f8a7dadb388215396ba5fbee2c610a04d6` |
| 硬件 | A3 560T、A3 752T、A5 Atlas 850E、Atlas 850 的上游画像 |
| 并行 | `global_queries = batch_per_DP × DP`，总 ranks = 机器数 × 每机 ranks |
| 量化 | 专家 W4A8_DYNAMIC，非专家 W8A8_DYNAMIC；与原网页 checkpoint 的权重口径不同 |
| Engram | Host 表与 H2D 搬运，单列 Host 容量 |
| Prefill | 完整 40 层参考路径，未采用 CED decoder 尾部回放；一次完整输入，无 chunk 调度 |
| Decode | 在 `input + floor((output−1)/2)` 上下文估算单周期；不是整段生成的逐周期积分 |
| DSpark | 原生三个草稿层、最多五个草稿位置；独立提交数假设，未预测接受分布 |
| 性能 | eager analytic，未启用 compile、多流、实测校准或自动 batch 搜索 |
| 显存 | 当前形状的峰值估计，不等同完整 Decode 输出末端峰值 |

页面会展示实际 `effective` 配置和上述差异。显存预算与时延条件分别显示；
有计算结果不代表配置满足容量或 SLO。原页面解析估计仍单独保留。

## 任务与资源

- 同时只运行一个任务；运行预算 180 秒，进程清理另有最多 6 秒预算。
- 仿真进程树 RAM 上限 1 GiB。超过上限或时间预算会终止任务并显式报错。
- 最多保留 12 个任务目录（含活跃任务），在提交新任务时清理旧目录，跨重启生效；不会上传到 GitHub。
- 浏览器关闭不会自动取消后台任务；取消按钮会请求回收进程，终态在进程退出后返回。
- 服务重启后旧页面的任务查询会返回不存在，旧产物可在本地状态目录查看。

接口：

```text
GET  /api/msmodeling/v1/health
POST /api/msmodeling/v1/jobs
GET  /api/msmodeling/v1/jobs/{id}
POST /api/msmodeling/v1/jobs/{id}/cancel
GET  /api/msmodeling/v1/jobs/{id}/trace
GET  /api/msmodeling/v1/jobs/{id}/log
```

只接受预定义 DSv4.1 工作负载，不接受任意模型路径或 Shell 命令。
跨源默认仅允许 `https://dragondream-chen.github.io` 与服务自身；
其他 fork 可用 `--allow-origin https://你的用户名.github.io` 指定其页面来源。

## 验证

```powershell
python -m unittest discover -s tests -p test_msmodeling_bridge.py
node tests/simulator-audit.test.cjs
node tests/weight-shapes.test.cjs
git diff --check
```

`tests/fixtures/msmodeling-ar-native.json` 来自上述固定上游版本的一次真实离线 AR 仿真输出，
用于验证单位、batch、吞吐折算及结果来源；它不是真机验收数据。

2026-11-11 在 Windows 本机完成了 AR、小形状与默认 16K/1K 的 P/DSpark 离线运行，
并验证浏览器提交、JSON 下载、Trace、取消和真实 worker 超时回收。
下表是 A5 850E 96 GiB、单机 EP8、batch/DP=1 的一次预测结果，供复现核对：

| 阶段 | TP / DP | 原生单批或单周期耗时 | 吞吐 | 每 rank 峰值显存 |
| --- | --- | --- | --- | --- |
| P 完整 40 层 | 8 / 1 | 534.10 ms | 1.872 req/s，30,676 输入 token/s | 37.834 GiB |
| D DSpark，平均提交 3 | 1 / 8 | 49.597 ms | 0.473 req/s，483.90 输出 token/s | 42.956 GiB |

D 的请求吞吐按 1,023 个后续输出 token 折算，不含 Prefill；这些数值不能代替设备实测。
