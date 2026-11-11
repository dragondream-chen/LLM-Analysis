"""[Role] Translate page inputs into an explicit TensorCast workload.
[Invariants] Batch is per DP; acceptance is independent of latency targets.
[Timeouts & Locks] Pure calculations; no network, locks, or subprocesses.
[Side-Effect Policy] Never change the upstream engine or load model weights.
"""

from __future__ import annotations

import math
from pathlib import Path


PROFILES = {
    "a3-560": ("ATLAS_800_A3_560T_128G_DIE_ROCE", 16, 64),
    "a3-752": ("ATLAS_800_A3_752T_128G_DIE", 16, 64),
    "a5-96": ("ATLAS_850E_486T_96G", 8, 96),
    "a5-128": ("ATLAS_850_486T_112G", 8, 112),
}


def number(value, name, low, high, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low}–{high} 之间")
    if integer and int(value) != value:
        raise ValueError(f"{name} 必须是整数")
    return int(value) if integer else float(value)


def workload(payload: dict, model_dir: Path, trace: Path) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise ValueError("需要 config 配置对象")
    c = payload["config"]
    phase = payload.get("phase")
    if phase not in ("p", "d"):
        raise ValueError("phase 必须是 p 或 d")
    batch = number(payload.get("batch"), "batch/DP", 1, 128, integer=True)
    input_len = number(c.get("input"), "输入长度", 1, 1048575, integer=True)
    output_len = number(c.get("output"), "输出长度", 1, 1048575, integer=True)
    if input_len + output_len > 1048576:
        raise ValueError("输入与输出之和不能超过 1,048,576 tokens")
    if phase == "d" and output_len == 1:
        raise ValueError("输出只有首 token，无需运行 Decode")
    for key, label in (("ttft", "TTFT"), ("itl", "ITL"), ("tpot", "TPOT")):
        number(c.get(key), label, .000001, 100000)
    decode = c.get("decode", "ar")
    if decode not in ("ar", "dspark"):
        raise ValueError("只支持 AR 或 DSpark")
    if c.get("vision"):
        raise ValueError("当前接入只运行纯文本；请取消 Vision 权重选项")
    s = c.get(phase)
    if not isinstance(s, dict) or s.get("hw") not in PROFILES:
        raise ValueError("当前服务支持 A3 560T/752T 和 A5 850/850E 四种画像")
    profile, ranks_per_node, capacity = PROFILES[s["hw"]]
    nodes = number(s.get("nodes"), "机器数", 1, 8, integer=True)
    tp = number(s.get("tp"), "TP", 1, 8, integer=True)
    ep = number(s.get("ep"), "EP", 1, 128, integer=True)
    world = nodes * ranks_per_node
    if tp not in (1, 2, 4, 8) or world % tp or ep not in (1, world):
        raise ValueError("TP 必须整除总 ranks；EP 必须为 1 或总 ranks")
    draft_tokens = min(5, output_len - 2) if phase == "d" and decode == "dspark" else 0
    if 384 % ep or (draft_tokens > 0 and 128 % ep):
        raise ValueError("EP 必须整除主模型 384 和 DSpark 128 个专家")
    dp = world // tp
    util = number(c.get("util", 85), "显存利用率", 1, 100)
    reserve = number(c.get("reserve", 4), "运行预留 GiB", 0, 1024)
    usable = number(c.get("usable", 0), "可见 GiB", 0, capacity) or capacity
    accepted = 1.0
    if phase == "d" and decode == "dspark":
        accepted = number(payload.get("acceptedTokensPerCycle", 3), "每周期提交 tokens", 1, min(6, output_len - 1))
    context = 0 if phase == "p" else input_len + (output_len - 1) // 2
    query = input_len if phase == "p" else draft_tokens + 1
    argv = [
        str(model_dir), "--device", profile, "--num-devices", str(world),
        "--tp-size", str(tp), "--dp-size", str(dp), "--ep-size", str(ep),
        "--num-queries", str(batch * dp), "--query-length", str(query),
        "--context-length", str(context), "--prefill" if phase == "p" else "--decode",
        "--quantize-linear-action", "W4A8_DYNAMIC",
        "--quantize-non-expert-linear-action", "W8A8_DYNAMIC",
        "--reserved-memory-gb", str(reserve), "--performance-model", "analytic",
        "--chrome-trace-file", str(trace),
    ]
    if draft_tokens:
        argv += ["--speculative-method", "dspark", "--num-speculative-tokens", str(draft_tokens), "--num-draft-layers", "3"]
    warnings = [
        "这是 msmodeling analytic 估计，未进行本次硬件实测或精度校准。",
        "本地桥接使用 W4A8 专家 / W8A8 非专家、Engram Host 与原生 CSA2 缓存；与网页 checkpoint 容量口径分别展示。",
        "本次为固定 batch 的 eager 仿真；未启用 torch.compile、多流或服务级排队搜索。",
    ]
    if phase == "p":
        warnings.append("Prefill 运行上游默认完整 40 层参考路径；没有使用网页 CED 尾 128 回放或 chunk 估计。")
    else:
        warnings.append("Decode 在生成区间中点上下文估算一个周期；显存峰值仅对应这个形状，不代表整段生成末端峰值。")
        if draft_tokens:
            warnings.append("每周期提交数由独立输入给定，未预测接受率；短输出末轮未做逐周期离散重放。")
    if c.get("unpack") or c.get("cache") == "reference":
        warnings.append("网页 BF16 展开/参考缓存选项不改变本次原生量化方案；请以有效配置为准。")
    if c.get("compute", 1) != 1 or c.get("memory", 1) != 1 or c.get("imbalance", 1.1) != 1.1:
        warnings.append("网页手动效率与负载偏斜系数不迁移；本次采用 msmodeling 硬件画像。")
    return {
        "phase": phase, "batch": batch, "input": input_len, "output": output_len,
        "checkpoint": "deepseek-ai/DeepSeek-V4.1-Flash",
        "checkpointRevision": "314113f8a7dadb388215396ba5fbee2c610a04d6",
        "decode": decode, "dp": dp, "tp": tp, "ep": ep, "world": world,
        "context": context, "queryLength": query, "globalQueries": batch * dp,
        "acceptedTokensPerCycle": accepted, "deviceProfile": profile,
        "capacityGiB": usable, "memoryBudgetGiB": usable * util / 100, "reserveGiB": reserve,
        "quantization": "W4A8_DYNAMIC experts / W8A8_DYNAMIC non-experts",
        "engramPlacement": "host", "executionPath": "full_40_layer_reference" if phase == "p" else "midpoint_decode_cycle",
        "performanceModel": "analytic", "compiled": False,
        "warnings": warnings, "argv": argv, "pageConfig": c,
    }


def normalize(native: dict, effective: dict, engine: dict, job_id: str) -> dict:
    times = native.get("execution_time_s", {})
    latency = times.get("analytic")
    if not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0:
        raise ValueError("msmodeling 未返回有效的 analytic execution_time_s")
    memory = native["memory_gb"]
    p = effective["phase"] == "p"
    accepted = effective["acceptedTokensPerCycle"]
    tpot = None if p else latency / accepted
    duration = latency if p else (effective["output"] - 1) * tpot
    qps = effective["globalQueries"] / duration
    ops = []
    for op in native.get("runtime_event_list", []):
        if op.get("perf_model") in (None, "analytic"):
            ops.append({"name": op.get("name", ""), "calls": op.get("call_times", 0), "totalSeconds": op.get("perf_total", 0)})
    ops.sort(key=lambda op: op["totalSeconds"], reverse=True)
    page = effective["pageConfig"]
    memory_ok = memory["peak_usage"] + effective["reserveGiB"] <= effective["memoryBudgetGiB"]
    slo_ok = latency <= page.get("ttft", 10) if p else latency <= page.get("itl", .06) and tpot <= page.get("tpot", .02)
    return {
        "source": "msmodeling", "measurement": False, "engine": engine,
        "phase": effective["phase"], "batch": effective["batch"], "latencySeconds": latency,
        "cycleSeconds": None if p else latency, "tpotSeconds": tpot,
        "requestQps": qps, "tokensPerSecond": qps * (effective["input"] if p else effective["output"] - 1),
        "acceptedTokensPerCycle": accepted, "peakMemoryGiB": memory["peak_usage"],
        "weightsGiB": memory["model_weight"], "kvGiB": memory["kv_cache"],
        "activationsGiB": memory["model_activation"], "memoryFeasible": memory_ok, "sloFeasible": slo_ok,
        "hostMemoryGiB": native.get("engram_host_resident_size_gb"),
        "hostMemoryPerRankGiB": native.get("engram_host_resident_per_rank_gb"),
        "hostToDeviceBytes": native.get("host_to_device_bytes"),
        "hostToDeviceSeconds": native.get("host_to_device_time_s"),
        "simulatorWallSeconds": native.get("run_time_s"),
        "executionPath": effective["executionPath"], "warnings": effective["warnings"],
        "effective": {k: v for k, v in effective.items() if k != "argv"}, "command": effective["argv"],
        "operators": ops, "traceUrl": f"/api/msmodeling/v1/jobs/{job_id}/trace",
        "logUrl": f"/api/msmodeling/v1/jobs/{job_id}/log",
    }
