"""[Role] Own bounded, cancellable local TensorCast processes.
[Invariants] A completed result comes only from a successful native worker.
[Timeouts & Locks] One worker; runtime <=180s plus two 3s cleanup waits.
[Side-Effect Policy] Write only job artifacts; never execute client shell text.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

import psutil

from .config import normalize, workload


def now():
    return datetime.now(timezone.utc).isoformat()


def stop(process):
    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        process.wait(timeout=3)
        return
    targets = [*children, root]
    for item in targets:
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=3)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise RuntimeError("仿真子进程未能在清理预算内退出")
    process.wait(timeout=1)


def tail(path, limit=65536):
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode("utf-8", errors="replace")


class Jobs:
    def __init__(self, root: Path, source: Path, python: Path, *, timeout=180, max_memory_mb=1024):
        self.root, self.source, self.python = root.resolve(), source.resolve(), python.resolve()
        self.state = self.root / ".local" / "msmodeling-jobs"
        self.state.mkdir(parents=True, exist_ok=True)
        self.timeout, self.max_memory_mb = timeout, max_memory_mb
        self.lock = threading.Lock()
        self.items = OrderedDict()
        self.thread = None
        self.engine = {"name": "msmodeling", "python": str(self.python), "source": str(self.source), "revision": None}
        self.ready, self.reason = self._probe()

    def _prune(self):
        directories = sorted(
            (p for p in self.state.iterdir() if p.is_dir() and len(p.name) == 32
             and all(c in "0123456789abcdef" for c in p.name)),
            key=lambda p: p.stat().st_mtime,
        )
        for directory in directories[:-11]:
            target = directory.resolve()
            if target.parent != self.state.resolve():
                raise RuntimeError("任务目录超出项目状态目录")
            shutil.rmtree(target)
            self.items.pop(directory.name, None)

    def _probe(self):
        if not self.python.is_file() or not (self.source / "cli/inference/text_generate.py").is_file():
            return False, "未找到 msmodeling 源码或项目 Python；请按接入指南完成安装"
        result = subprocess.run(
            [str(self.python), "-I", "-c", "import torch, transformers, psutil; print(torch.__version__); print(transformers.__version__)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return False, "项目依赖未能导入：" + (result.stderr or result.stdout)[-3000:]
        self.engine["dependencies"] = result.stdout.strip().splitlines()
        revision = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if revision.returncode:
            return False, "无法读取 msmodeling Git 修订"
        self.engine["revision"] = revision.stdout.strip()
        return True, None

    def health(self):
        with self.lock:
            active = next((x["id"] for x in self.items.values() if x["status"] in ("queued", "running")), None)
        return {"ready": self.ready, "reason": self.reason, "engine": self.engine, "activeJob": active,
                "warnings": ["固定 batch / eager / analytic；不读取模型权重，不运行 NPU。", "Prefill 为上游默认完整 40 层参考路径。"]}

    @staticmethod
    def public(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def get(self, job_id):
        with self.lock:
            return self.public(self.items[job_id])

    def create(self, payload):
        if not self.ready:
            raise RuntimeError(self.reason)
        job_id = uuid.uuid4().hex
        directory = self.state / job_id
        effective = workload(payload, self.root / "data/msmodeling/deepseek-v4.1-flash", directory / "trace.json")
        with self.lock:
            if any(x["status"] in ("queued", "running") for x in self.items.values()):
                raise RuntimeError("已有任务正在运行；请等待完成或取消后再提交")
            self._prune()
            directory.mkdir()
            (directory / "request.json").write_text(json.dumps(effective, ensure_ascii=False), encoding="utf-8")
            job = {"id": job_id, "status": "queued", "phase": effective["phase"], "batch": effective["batch"],
                   "createdAt": now(), "updatedAt": now(), "error": None, "result": None,
                   "_cancel": threading.Event(), "_directory": directory}
            self.items[job_id] = job
            self.thread = threading.Thread(target=self._run, args=(job, effective), daemon=True)
            self.thread.start()
            return self.public(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.items[job_id]
            job["_cancel"].set()
            return self.public(job)

    def artifact(self, job_id, kind):
        with self.lock:
            job = self.items[job_id]
            return job["_directory"] / {"trace": "trace.json", "log": "worker.log"}[kind]

    def _update(self, job, **values):
        with self.lock:
            job.update(values, updatedAt=now())

    def _run(self, job, effective):
        directory = job["_directory"]
        process = None
        try:
            self._update(job, status="running")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", CUDA_VISIBLE_DEVICES="", ASCEND_RT_VISIBLE_DEVICES="")
            command = [str(self.python), "-I", str(self.root / "msmodeling_bridge/worker.py"),
                       "--source", str(self.source), "--request", str(directory / "request.json"),
                       "--result", str(directory / "native.json")]
            started = time.monotonic()
            problem = None
            peak_rss = 0
            with (directory / "worker.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=self.source, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                while process.poll() is None:
                    if job["_cancel"].is_set():
                        problem = "cancelled"
                    elif time.monotonic() - started > self.timeout:
                        problem = f"仿真超过 {self.timeout} 秒预算，进程已终止"
                    try:
                        item = psutil.Process(process.pid)
                        rss = item.memory_info().rss + sum(x.memory_info().rss for x in item.children(recursive=True))
                        peak_rss = max(peak_rss, rss)
                        if rss > self.max_memory_mb * 1024**2:
                            problem = f"本机仿真内存超过 {self.max_memory_mb} MiB 预算，进程已终止"
                    except psutil.NoSuchProcess:
                        pass
                    if problem:
                        stop(process)
                        break
                    job["_cancel"].wait(.15)
                process.wait(timeout=1)
            if problem == "cancelled":
                self._update(job, status="cancelled", error="用户取消；仿真进程已退出")
                return
            if problem:
                raise RuntimeError(problem)
            if process.returncode:
                raise RuntimeError(tail(directory / "worker.log", 6000).strip() or f"worker exit {process.returncode}")
            native_path = directory / "native.json"
            if native_path.stat().st_size > 16 * 1024**2:
                raise RuntimeError("原生指标文件超过 16 MiB，拒绝整体载入")
            native = json.loads(native_path.read_text(encoding="utf-8"))
            result = normalize(native, effective, self.engine, job["id"])
            result["hostPeakMemoryMiB"] = peak_rss / 1024**2
            (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            self._update(job, status="completed", result=result)
        except Exception as exc:
            if process is not None and process.poll() is None:
                try:
                    stop(process)
                except Exception as cleanup:
                    self._update(job, status="failed", error=f"{exc}\n进程清理失败：{cleanup}")
                    return
            self._update(job, status="failed", error=str(exc))

    def close(self):
        with self.lock:
            for job in self.items.values():
                if job["status"] in ("queued", "running"):
                    job["_cancel"].set()
        if self.thread:
            self.thread.join(timeout=8)
