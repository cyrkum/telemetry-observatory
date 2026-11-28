"""Read and write telemetry; MongoDB TTL enforces sample retention."""
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient

from policy import DEFAULT_DASHBOARD_PORT, DEFAULT_FREQUENCY_SECONDS, DEFAULT_RETENTION_DAYS, expiry_for, validate_policy

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
samples = client.observatory.samples
settings = client.observatory.settings
audit_path = Path(os.getenv("AUDIT_PATH", "/audits/api.jsonl"))
audit_lock = threading.Lock()


def audit(action, status, detail=None):
    record = {"at": datetime.now(timezone.utc).isoformat(), "service": "api", "action": action, "status": status, "detail": detail or {}}
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_lock, audit_path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def require_admin(header):
    expected = os.getenv("ADMIN_TOKEN", "")
    supplied = (header or "").removeprefix("Bearer ")
    if len(expected) < 24 or not secrets.compare_digest(supplied, expected):
        audit("authorization", "denied")
        raise HTTPException(401, "Invalid administrator token")


def active_port():
    return int(os.getenv("DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT))


def current_policy():
    doc = settings.find_one({"_id": "global"}) or {}
    return {"frequency_seconds": doc.get("frequency_seconds", DEFAULT_FREQUENCY_SECONDS), "retention_days": doc.get("retention_days", DEFAULT_RETENTION_DAYS)}


def configuration():
    doc = settings.find_one({"_id": "global"}) or {}
    port = doc.get("dashboard_port", active_port())
    return {"frequency_seconds": doc.get("frequency_seconds", DEFAULT_FREQUENCY_SECONDS), "retention_days": doc.get("retention_days", DEFAULT_RETENTION_DAYS), "dashboard_port": port, "active_dashboard_port": active_port(), "restart_required": port != active_port()}


def request_restart(port):
    req = urllib.request.Request(
        os.getenv("CONTROL_URL", "http://host.docker.internal:8766").rstrip("/") + "/restart",
        data=json.dumps({"dashboard_port": port}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.getenv("ADMIN_TOKEN", "")},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 202
    except urllib.error.HTTPError as exc:
        raise HTTPException(502 if exc.code >= 500 else exc.code, "Restart agent rejected the request") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HTTPException(503, "Restart agent is unavailable; is host_control.py running?") from exc


class Sample(BaseModel):
    host: str = Field(min_length=1, max_length=128)
    sampled_at: datetime
    cpu_percent: float = Field(ge=0, le=100)
    gpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory: dict
    swap: dict
    disks: list[dict]
    disk_io: dict
    network_io: dict
    applications: list[dict]
    battery: dict | None = None
    local_ai: dict | None = None


@asynccontextmanager
async def lifespan(app):
    samples.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    samples.create_index([("sampled_at", DESCENDING)])
    audit("startup", "ok")
    yield
    audit("shutdown", "ok")


app = FastAPI(title="Telemetry Observatory", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/api/health")
def health():
    client.admin.command("ping")
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings():
    return configuration()


@app.put("/api/settings")
def put_settings(value: dict, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    try:
        policy = validate_policy(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    data = {"frequency_seconds": policy.frequency_seconds, "retention_days": policy.retention_days}
    if policy.dashboard_port is not None:
        data["dashboard_port"] = policy.dashboard_port
    settings.update_one({"_id": "global"}, {"$set": data}, upsert=True)
    # Recalculate existing expiry dates when the retention policy changes.
    for item in samples.find({}, {"_id": 1, "sampled_at": 1}):
        at = item["sampled_at"].replace(tzinfo=timezone.utc)
        samples.update_one({"_id": item["_id"]}, {"$set": {"expires_at": expiry_for(at, policy.retention_days)}})
    audit("settings.update", "ok", data)
    return configuration()


@app.post("/api/restart", status_code=202)
def restart(authorization: str | None = Header(default=None)):
    require_admin(authorization)
    port = configuration()["dashboard_port"]
    try:
        request_restart(port)
    except HTTPException as exc:
        audit("services.restart", "error", {"detail": exc.detail})
        raise
    audit("services.restart", "requested", {"dashboard_port": port})
    return {"status": "restarting", "dashboard_port": port}


@app.post("/api/samples", status_code=201)
def post_sample(value: Sample, authorization: str | None = Header(default=None)):
    require_admin(authorization)
    at = value.sampled_at.astimezone(timezone.utc)
    if abs((datetime.now(timezone.utc) - at).total_seconds()) > 300:
        raise HTTPException(422, "Sample clock is more than five minutes from server clock")
    item = value.model_dump()
    item["sampled_at"] = at
    item["expires_at"] = expiry_for(at, current_policy()["retention_days"])
    samples.insert_one(item)
    audit("sample.ingest", "ok", {"host": value.host})
    return {"status": "stored"}


@app.get("/api/samples")
def get_samples(limit: int = Query(120, ge=1, le=1000)):
    result = []
    for doc in samples.find({}, {"_id": 0, "expires_at": 0}).sort("sampled_at", DESCENDING).limit(limit):
        doc["sampled_at"] = doc["sampled_at"].replace(tzinfo=timezone.utc).isoformat()
        result.append(doc)
    return result


HISTORY_MAX_POINTS = 360


def history_bucket_seconds(hours):
    """Bucket width that keeps a history range at or under HISTORY_MAX_POINTS points."""
    return max(10, -(-hours * 3600 // HISTORY_MAX_POINTS))


@app.get("/api/history")
def get_history(hours: int = Query(1, ge=1, le=720)):
    """CPU, GPU, and memory averages per time bucket, newest first, for long chart ranges."""
    bucket = history_bucket_seconds(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    pipeline = [
        {"$match": {"sampled_at": {"$gte": since}}},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$sampled_at", "unit": "second", "binSize": bucket}},
            "cpu_percent": {"$avg": "$cpu_percent"},
            "gpu_percent": {"$avg": "$gpu_percent"},
            "memory_percent": {"$avg": "$memory.percent"},
            "samples": {"$sum": 1},
        }},
        {"$sort": {"_id": DESCENDING}},
    ]
    return [
        {
            "sampled_at": row["_id"].replace(tzinfo=timezone.utc).isoformat(),
            "cpu_percent": round(row["cpu_percent"] or 0, 1),
            "gpu_percent": round(row["gpu_percent"], 1) if row["gpu_percent"] is not None else None,
            "memory": {"percent": round(row["memory_percent"] or 0, 1)},
            "samples": row["samples"],
        }
        for row in samples.aggregate(pipeline)
    ]


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    try:
        response = await call_next(request)
        if request.method != "GET" or response.status_code >= 400:
            audit("http.request", str(response.status_code), {"method": request.method, "path": request.url.path})
        return response
    except Exception as exc:
        audit("http.request", "error", {"path": request.url.path, "type": type(exc).__name__})
        raise
