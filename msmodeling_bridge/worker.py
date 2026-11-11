"""[Role] Execute one genuine upstream TensorCast workload in isolation.
[Invariants] Native structured metrics are the only source of estimates.
[Timeouts & Locks] The parent bounds runtime and terminates this process.
[Side-Effect Policy] Local metadata only; no weights, devices, or remote writes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    effective = json.loads(args.request.read_text(encoding="utf-8"))
    sys.path.insert(0, str(args.source.resolve()))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch

    torch.set_num_threads(1)
    from cli.inference.text_generate import arg_parse, align_decode_query_length
    from tensor_cast.core.compilation_config import apply_compilation_config
    from tensor_cast.core.input_generator import generate_inputs
    from tensor_cast.core.model_runner import ModelRunner
    from tensor_cast.core.user_config import UserInputConfig

    sys.argv = ["text_generate", *effective["argv"]]
    options = arg_parse()
    align_decode_query_length(options)
    apply_compilation_config(options.compilation_config)
    user_input = UserInputConfig.from_args(options)
    print("[msmodeling] constructing meta model", flush=True)
    runner = ModelRunner(user_input)
    print("[msmodeling] running analytic workload", flush=True)
    metrics = runner.run_inference(generate_inputs_func=generate_inputs)
    metrics.dump_json(str(args.result))
    payload = json.loads(args.result.read_text(encoding="utf-8"))
    for key in ("engram_host_resident_size_gb", "engram_host_resident_per_rank_gb", "host_to_device_bytes", "host_to_device_time_s"):
        payload[key] = getattr(metrics, key, None)
    args.result.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print("[msmodeling] completed", flush=True)


if __name__ == "__main__":
    main()
