# Design document

## Purpose and scope

Telemetry Observatory collects resource samples from one macOS host, stores them in MongoDB, and displays recent data in a local web dashboard. The native sensor is required because Docker Desktop containers run in a Linux VM and cannot report the macOS host's counters directly. This is a single-host observability tool, not a remote fleet or alerting system.

## Architecture

```text
[macOS sensor] -- GET /sample --> [Poller] -- POST sample --> [API] <--> [MongoDB]
                                                               ^
[Browser] -- localhost:8080 --> [Nginx dashboard] -- /api proxy-+
                                                               |
[Control agent] <-- POST /restart (ADMIN_TOKEN) ---------------+
       `-- writes .env, runs `docker compose up -d --force-recreate`
```

The sensor and the control agent are host processes managed by `launchd`. The other four services are defined in `compose.yaml` and communicate over a private Compose network. Only the dashboard port is published to the host, bound to loopback by default (`127.0.0.1:8080`). Nginx serves the static page and proxies `/api/` requests to the API.

## Components and responsibilities

| Component | Responsibility |
| --- | --- |
| `host_sensor.py` | Reads host metrics using `psutil` and macOS `ioreg`; serves a token-protected `GET /sample` response. |
| `poller/main.py` | Reads current policy, fetches a sensor sample, and submits it to the API on the configured schedule. |
| `api/main.py` | Validates and stores samples, returns recent samples/settings, authorizes writes, and manages retention expiry. |
| MongoDB | Stores time-stamped samples and policy settings; expires samples using a TTL index. |
| `host_control.py` | Host agent that validates the dashboard port, updates `DASHBOARD_PORT` in `.env`, and runs `docker compose up -d --force-recreate`. |
| `web/` | Static dashboard UI; fetches API data and submits configuration (policy, port) and restart requests from a modal form. |
| Nginx | Serves the UI and proxies API paths within the Compose network. |

## Data flow

1. The sensor collects CPU use, GPU device utilization, virtual and swap memory, mounted volumes and disk usage, cumulative disk I/O, network counters, and process data. GPU utilization comes from the first valid `Device Utilization %` in `IOAccelerator` performance statistics via `ioreg`; unsupported or failed reads are audited and returned as `null`. It skips processes or mounts that macOS does not permit it to read. It returns the union of the twenty largest processes by memory and the twenty busiest by CPU (at most forty entries), so the UI can show the top twelve by either value. It also reports battery charge, power state and estimated time remaining, or `null` on Macs without a battery. Under `local_ai` it reports Ollama reachability, version and loaded models (size, GPU-resident share, expiry) from `OLLAMA_URL`, local-runtime processes, and a 24-hour local/remote summary of the token-telemetry JSONL log at `TOKEN_TELEMETRY_PATH` (`null` when absent). An unreachable Ollama is audited as `ollama.read` and reported as `reachable: false`.
2. The poller reads `GET /api/settings` to get its interval, requests `GET /sample` with `X-Sensor-Token`, then posts the resulting JSON to `POST /api/samples` using `Authorization: Bearer <ADMIN_TOKEN>`.
3. The API validates the sample shape and timestamp (within five minutes of the API clock), records it with an `expires_at` calculated from the current retention setting, and stores it in MongoDB.
4. The dashboard fetches recent samples and settings through Nginx. The page refreshes sample data every five seconds. Disk rates are computed in the browser from the difference between the newest two cumulative disk I/O records.
5. The API creates a MongoDB TTL index on `expires_at`. Changing retention recalculates existing records' expiry values; TTL cleanup itself is asynchronous.

The sample payload includes host name, sample time, CPU, nullable GPU percentage, memory, swap, disks, disk I/O, network I/O, applications, battery, and nullable `local_ai`. Older samples may omit GPU utilization and `local_ai`. MongoDB stores samples in `observatory.samples` and the global polling/retention policy and saved dashboard port in `observatory.settings`.

### Configuration and restart flow

1. The Configuration form saves frequency, retention, and dashboard port through `PUT /api/settings`. The API compares the saved port with the port the running stack was started with (`DASHBOARD_PORT` passed to the API container) and reports `restart_required`; the form shows a warning while they differ.
2. **Restart services** saves the form, then calls `POST /api/restart`. The API forwards the saved port to the control agent at `CONTROL_URL` (`http://host.docker.internal:${CONTROL_PORT:-8766}`) with the administrator token.
3. The agent re-reads `ADMIN_TOKEN` from `.env`, validates the port (integer, 1024–65535), atomically rewrites `DASHBOARD_PORT`, replies `202`, then recreates the services in a background thread. Only one restart runs at a time (`409` otherwise). Because the API itself is recreated, the browser redirects to the new port after a short delay.
4. The agent is a separate host process because containers cannot run `docker compose` without a Docker socket mount, which would grant the API root-equivalent control of the host.

## HTTP interfaces

### Host sensor

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `GET /sample` | `X-Sensor-Token` header | Return one host sample. Other paths return 404; an invalid or too-short configured token is rejected. |

The sensor binds to `0.0.0.0` by default so Docker Desktop can reach the host service. It is not published by Compose; host firewall rules should be considered on untrusted networks.

### Host control agent

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `POST /restart` | `Authorization: Bearer <ADMIN_TOKEN>` | Optional JSON body `{"dashboard_port": N}` (max 1024 bytes). Returns `202` when a restart starts; `400` invalid input, `401` bad token, `409` restart in progress, `413` body too large. Other paths return 404. |

The agent binds to `127.0.0.1` by default (`CONTROL_BIND`, `CONTROL_PORT`, default 8766); Docker Desktop reaches it via `host.docker.internal`.

### API

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `GET /api/health` | None | Ping MongoDB and report API health. |
| `GET /api/settings` | None | Return current policy, defaulting to 10 seconds and 7 days before a policy is saved. |
| `PUT /api/settings` | `Authorization: Bearer <ADMIN_TOKEN>` | Validate and save the 2–3600 second polling interval, 1–365 day retention period, and dashboard port (1024–65535). `GET` also reports `active_dashboard_port` and `restart_required`. |
| `POST /api/restart` | `Authorization: Bearer <ADMIN_TOKEN>` | Ask the host control agent (`host_control.py`) to apply the saved dashboard port and recreate the Compose services. |
| `POST /api/samples` | `Authorization: Bearer <ADMIN_TOKEN>` | Validate, timestamp, and store a sample. |
| `GET /api/samples?limit=N` | None | Return newest samples, default 120, with allowed limits from 1 to 1000. |
| `GET /api/history?hours=N` | None | Return CPU, GPU and memory averages per time bucket for the last N hours (1 to 720, default 1), newest first. GPU averages exclude unavailable readings and are null when no readings are available. MongoDB groups samples with `$dateTrunc` into buckets of at least 10 seconds, sized for at most 360 points. |

The API's interactive OpenAPI/Redoc documentation endpoints are disabled. The API has no published host port in the Compose configuration; the dashboard reaches it through Nginx.

## Configuration and dependencies

Compose reads `ADMIN_TOKEN`, `SENSOR_TOKEN`, `SENSOR_PORT`, `DASHBOARD_PORT`, and `CONTROL_PORT` from `.env`. The control agent rewrites `DASHBOARD_PORT` when the port is changed in the UI. The two tokens must be different and at least 24 characters long. `MONGO_URI`, `API_URL`, `SENSOR_URL`, and audit paths are set for container-to-container or host-to-container communication in `compose.yaml`.

Python package pins and acquisition instructions are listed in the [README](../README.md). Compose builds the Python API and poller images and pulls MongoDB and Nginx images. The host sensor and tests use the local Python virtual environment.

## Persistence, logging, and operations

- `mongo_data` is a named Docker volume that survives `docker compose down`. `docker compose down -v` removes it.
- The project `audits/` directory is mounted into the containers. Services append JSONL or Nginx/MongoDB log output there.
- `scripts/install-sensor.sh` generates `.sensor.launchd.plist` from the current project path and `.env`, then bootstraps the service. Re-run it after changing the path or sensor configuration. `scripts/install-control.sh` does the same for the control agent (`.control.launchd.plist`, console log `audits/control-console.log`) and records the `docker` location in its `PATH`.
- The local dashboard is not configured for remote exposure, TLS termination, or multi-user access.

## Security boundaries and limitations

- Store `.env`, the generated launchd plist, audit logs, and database backups as private files; tokens grant access to data ingestion, policy changes, and service restarts.
- The dashboard is loopback-bound by default. The native sensor listens on all host interfaces unless `SENSOR_BIND` is overridden, and protects sample access with `SENSOR_TOKEN`. The control agent is loopback-bound, authenticates every request with `ADMIN_TOKEN`, and is the only component that can restart the stack; the API holds no Docker access.
- The API read endpoints are unauthenticated, but are available to the dashboard through the local Nginx proxy and are not directly published as host ports by Compose.
- Collection is limited by permissions macOS grants to the sensor. Process CPU can be zero on an initial observation.
- TTL expiration is asynchronous, sample charts show recent stored data, and there is no remote-host aggregation, alerting, or backup automation.
