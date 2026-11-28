"""Native macOS adapter. Docker Desktop cannot see macOS host counters."""
import json
import math
import os
import plistlib
import secrets
import socket
import subprocess
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.parsers.expat import ExpatError

import psutil

audit_path = Path(os.getenv("SENSOR_AUDIT_PATH", str(Path(__file__).parent / "audits/sensor.jsonl")))
audit_lock = threading.Lock()


def audit(action, status, detail=None):
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_lock, audit_path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "service": "sensor", "action": action, "status": status, "detail": detail or {}}, separators=(",", ":")) + "\n")


def counter(value):
    return value._asdict() if value is not None else {}


def battery():
    """Battery state, or None on Macs without a battery."""
    read = getattr(psutil, "sensors_battery", None)
    value = read() if read else None
    if value is None:
        return None
    seconds = value.secsleft
    known = isinstance(seconds, (int, float)) and seconds >= 0
    return {"percent": round(value.percent, 1), "plugged": value.power_plugged, "seconds_left": int(seconds) if known else None}


def top_applications(applications, limit=20):
    """Union of the top processes by memory and by CPU, so the dashboard can sort by either."""
    chosen = {}
    for key in ("memory_bytes", "cpu_percent"):
        for item in sorted(applications, key=lambda app: app[key], reverse=True)[:limit]:
            chosen.setdefault(item["pid"], item)
    return sorted(chosen.values(), key=lambda app: app["memory_bytes"], reverse=True)


def gpu_percent():
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IOAccelerator", "-d", "1", "-a"],
            capture_output=True, check=True, timeout=5,
        )
        devices = plistlib.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, plistlib.InvalidFileException, ExpatError, ValueError, TypeError) as exc:
        audit("gpu.read", "error", {"type": type(exc).__name__})
        return None

    if isinstance(devices, list):
        for device in devices:
            stats = device.get("PerformanceStatistics") if isinstance(device, dict) else None
            value = stats.get("Device Utilization %") if isinstance(stats, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 and math.isfinite(value):
                return value
    audit("gpu.read", "error", {"message": "GPU utilization unavailable"})
    return None


LOCAL_AI_PROCESS_NAMES = ("ollama", "llama-server", "llama.cpp", "lm studio", "lmstudio", "mlx_lm", "localai")


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def ollama_status(base=None, timeout=1.5):
    """Ollama version and loaded models, with the share of each model held in GPU memory."""
    base = (base or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/version", timeout=timeout) as response:
            version = json.loads(response.read())
        with urllib.request.urlopen(f"{base}/api/ps", timeout=timeout) as response:
            ps = json.loads(response.read())
    except (OSError, ValueError) as exc:
        audit("ollama.read", "error", {"type": type(exc).__name__})
        return {"reachable": False, "version": None, "models": []}
    models = []
    listed = ps.get("models") if isinstance(ps, dict) else None
    for model in listed if isinstance(listed, list) else []:
        if not isinstance(model, dict):
            continue
        details = model.get("details") if isinstance(model.get("details"), dict) else {}
        size, vram = _int(model.get("size")), _int(model.get("size_vram"))
        models.append({
            "name": str(model.get("name") or "unknown"), "size": size, "size_vram": vram,
            "gpu_percent": round(min(100, 100 * vram / size), 1) if size and isinstance(model.get("size_vram"), (int, float)) and not isinstance(model.get("size_vram"), bool) and model["size_vram"] >= 0 else None,
            "expires_at": model.get("expires_at") if isinstance(model.get("expires_at"), str) else None,
            "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
        })
    version = version.get("version") if isinstance(version, dict) else None
    return {"reachable": True, "version": version if isinstance(version, str) else None, "models": models}


_TOKEN_LOGS = {}
_TOKEN_LOGS_LOCK = threading.Lock()


def _token_record(raw):
    """(timestamp, location, tokens or None, model) for one invocation line, or None to skip it."""
    try:
        record = json.loads(raw.decode("utf-8"))
        at = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("record_type") != "invocation":
        return None
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    where = record.get("execution_location") if record.get("execution_location") in ("local", "remote") else "unknown"
    tokens = record.get("total_tokens")
    tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None
    return at, where, tokens, record.get("model")


def _token_log_checkpoint(log, offset, size=512):
    """The first and last bytes already consumed, used to detect a log truncated in place and regrown."""
    log.seek(0)
    head = log.read(min(size, offset))
    log.seek(max(0, offset - size))
    return head, log.read(min(size, offset))


def token_usage(path=None, hours=24, now=None):
    """Local vs remote model invocations and tokens from the token-telemetry JSONL log, or None without a log.

    The log is read incrementally: each call parses only the bytes appended since the previous call and keeps
    the records still inside the window, so collection cost does not grow with the log's full history. A
    replaced or truncated file (log rotation) is read again from the start.
    """
    path = Path(path or os.getenv("TOKEN_TELEMETRY_PATH", str(Path.home() / ".agents/data/token-telemetry/telemetry.jsonl"))).expanduser()
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    with _TOKEN_LOGS_LOCK:
        try:
            with path.open("rb") as log:
                stat = os.fstat(log.fileno())
                state = _TOKEN_LOGS.get(str(path))
                if (state is None or state["inode"] != (stat.st_dev, stat.st_ino) or stat.st_size < state["offset"]
                        or _token_log_checkpoint(log, state["offset"]) != state["checkpoint"]):
                    state = {"inode": (stat.st_dev, stat.st_ino), "offset": 0, "records": [], "checkpoint": (b"", b"")}
                log.seek(state["offset"])
                for raw in log:
                    record = _token_record(raw)
                    if not raw.endswith(b"\n") and record is None:
                        break  # a line still being written; read it on a later call
                    state["offset"] += len(raw)
                    if record and record[0] >= since:
                        state["records"].append(record)
                state["checkpoint"] = _token_log_checkpoint(log, state["offset"])
        except OSError:
            _TOKEN_LOGS.pop(str(path), None)
            return None
        state["records"] = [record for record in state["records"] if record[0] >= since]
        _TOKEN_LOGS[str(path)] = state
        records = list(state["records"])
    totals = {key: {"invocations": 0, "tokens": 0, "unmeasured": 0} for key in ("local", "remote", "unknown")}
    last_at, last_model = None, None
    for at, where, tokens, model in records:
        totals[where]["invocations"] += 1
        if tokens is None:
            totals[where]["unmeasured"] += 1
        else:
            totals[where]["tokens"] += tokens
        if where == "local" and (last_at is None or at > last_at):
            last_at, last_model = at, model
    measured = totals["local"]["tokens"] + totals["remote"]["tokens"]
    return {"window_hours": hours, **totals,
            "local_share": round(100 * totals["local"]["tokens"] / measured, 1) if measured else None,
            "last_local_at": last_at.isoformat() if last_at else None,
            "last_local_model": last_model if isinstance(last_model, str) else None}


def local_ai(applications):
    """Local model runtimes: Ollama state, inference processes, and token-telemetry usage."""
    processes = [app for app in applications if any(name in app["name"].lower() for name in LOCAL_AI_PROCESS_NAMES)]
    processes.sort(key=lambda app: app["cpu_percent"], reverse=True)
    return {"ollama": ollama_status(), "processes": processes[:8], "tokens": token_usage()}


def collect():
    disks = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({"mount": part.mountpoint, "device": part.device, "filesystem": part.fstype, "total": usage.total, "used": usage.used, "free": usage.free, "percent": usage.percent})
        except (OSError, PermissionError):
            pass
    applications = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            applications.append({"pid": info["pid"], "name": info["name"] or "unknown", "memory_bytes": info["memory_info"].rss if info["memory_info"] else 0, "cpu_percent": info["cpu_percent"] or 0})
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    return {"host": socket.gethostname(), "sampled_at": datetime.now(timezone.utc).isoformat(), "cpu_percent": psutil.cpu_percent(interval=0.1), "gpu_percent": gpu_percent(), "memory": counter(psutil.virtual_memory()), "swap": counter(psutil.swap_memory()), "disks": disks, "disk_io": counter(psutil.disk_io_counters()), "network_io": counter(psutil.net_io_counters()), "applications": top_applications(applications), "battery": battery(), "local_ai": local_ai(applications)}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/sample":
            self.send_error(404)
            return
        expected = os.getenv("SENSOR_TOKEN", "")
        if len(expected) < 24 or not secrets.compare_digest(self.headers.get("X-Sensor-Token", ""), expected):
            audit("sample.read", "denied")
            self.send_error(401)
            return
        try:
            body = json.dumps(collect()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            audit("sample.read", "ok")
        except Exception as exc:
            audit("sample.read", "error", {"type": type(exc).__name__})
            self.send_error(500)

    def log_message(self, format, *args):
        pass


def main():
    if len(os.getenv("SENSOR_TOKEN", "")) < 24:
        raise SystemExit("Set SENSOR_TOKEN to at least 24 characters")
    server = ThreadingHTTPServer((os.getenv("SENSOR_BIND", "0.0.0.0"), int(os.getenv("SENSOR_PORT", "8765"))), Handler)
    audit("startup", "ok", {"port": server.server_port})
    try:
        server.serve_forever()
    finally:
        server.server_close()
        audit("shutdown", "ok")


if __name__ == "__main__":
    main()
