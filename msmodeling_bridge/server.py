"""[Role] Serve the existing page and local TensorCast job API.
[Invariants] Loopback only; public Pages is an explicitly allowed origin.
[Timeouts & Locks] Requests <=10s; jobs run asynchronously with bounded budgets.
[Side-Effect Policy] No arbitrary commands, model IDs, or client file paths.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import unquote, urlsplit

from .jobs import Jobs


PREFIX = "/api/msmodeling/v1/"


def handler(root, jobs, port, page_origin):
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", page_origin}
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def allowed(self):
            return self.headers.get("Host") in hosts and self.headers.get("Origin", "") in origins | {""}

        def headers_for(self, code, kind):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def json(self, value, code=200):
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.headers_for(code, "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):
            if not self.allowed():
                self.json({"error": "Origin / Host 不在本地服务允许范围"}, 403)
                return
            self.headers_for(204, "text/plain")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self.allowed():
                self.json({"error": "Origin / Host 不在本地服务允许范围"}, 403)
                return
            path = unquote(urlsplit(self.path).path)
            try:
                if path == PREFIX + "health":
                    self.json(jobs.health())
                    return
                if path.startswith(PREFIX + "jobs/"):
                    bits = path[len(PREFIX + "jobs/"):].split("/")
                    if len(bits) == 1:
                        self.json(jobs.get(bits[0]))
                    elif len(bits) == 2 and bits[1] in ("trace", "log"):
                        self.file(jobs.artifact(bits[0], bits[1]))
                    else:
                        self.json({"error": "未知任务接口"}, 404)
                    return
                relative = Path(path.lstrip("/") or "index.html")
                if any(part.startswith(".") for part in relative.parts) or relative.parts[0] not in {
                    "index.html", "glm53_prefill_decode_operator_block_diagram.html", "src", "data", "assets", "docs"
                }:
                    self.json({"error": "文件不在网页发布目录"}, 404)
                    return
                target = (root / relative).resolve()
                if not target.is_relative_to(root):
                    self.json({"error": "无效路径"}, 400)
                    return
                self.file(target)
            except (KeyError, FileNotFoundError):
                self.json({"error": "任务或文件不存在"}, 404)

        def file(self, target):
            if not target.is_file():
                raise FileNotFoundError(target)
            self.headers_for(200, (mimetypes.guess_type(target.name)[0] or "application/octet-stream") + ("; charset=utf-8" if target.suffix in (".html", ".js", ".css", ".json", ".log", ".md") else ""))
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, 65536)

        def do_POST(self):
            if not self.allowed():
                self.json({"error": "Origin / Host 不在本地服务允许范围"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("需要小于 64 KiB 的 application/json 请求")
                payload = json.loads(self.rfile.read(length))
                path = urlsplit(self.path).path
                if path == PREFIX + "jobs":
                    self.json(jobs.create(payload), 202)
                elif path.startswith(PREFIX + "jobs/") and path.endswith("/cancel"):
                    self.json(jobs.cancel(path[len(PREFIX + "jobs/"):-len("/cancel")]))
                else:
                    self.json({"error": "未知任务接口"}, 404)
            except (ValueError, TypeError) as exc:
                self.json({"error": str(exc)}, 400)
            except KeyError:
                self.json({"error": "任务不存在"}, 404)
            except RuntimeError as exc:
                self.json({"error": str(exc)}, 409)

    return Handler


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="LLM Analysis 本机 msmodeling 服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--msmodeling-path", type=Path, default=root / ".local/msmodeling")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--allow-origin", default="https://dragondream-chen.github.io")
    args = parser.parse_args()
    if args.python is None:
        candidates = [args.msmodeling_path / ".venv/Scripts/python.exe", args.msmodeling_path / ".venv/bin/python"]
        args.python = next((p for p in candidates if p.is_file()), candidates[0])
    jobs = Jobs(root, args.msmodeling_path, args.python)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(root, jobs, args.port, args.allow_origin))
    print(f"http://127.0.0.1:{args.port}/?model=deepseek-v4.1-flash#performance", flush=True)
    if not jobs.ready:
        print(jobs.reason, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.close()
        server.server_close()


if __name__ == "__main__":
    main()
