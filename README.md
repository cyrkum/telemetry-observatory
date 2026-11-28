# Telemetry Observatory

A local dashboard for CPU, GPU, memory, disk activity, storage, and processes on one macOS host. Open <http://127.0.0.1:8080> after setup.

## Requirements and dependencies

- macOS
- Python 3.11 or newer for the host sensor and local tests. Install it from [python.org](https://www.python.org/downloads/macos/) or a trusted package manager.
- Node.js for the dashboard chart and hover-tooltip regression tests; they are skipped when Node.js is unavailable.
- [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/) with Docker Compose v2, running before startup.
- Network access on first setup to obtain Python packages from PyPI and container images from Docker Hub.

Python packages are pinned in the requirements files:

| Component | Dependencies | Obtain/install |
| --- | --- | --- |
| macOS host sensor | `psutil==7.0.0` | Installed from PyPI by `python3 -m pip install -r requirements-dev.txt` |
| API | `fastapi==0.115.12`, `uvicorn==0.34.2`, `pymongo==4.12.1` | Installed from PyPI when the API image is built |
| Poller | `requests==2.32.3` | Installed from PyPI when the poller image is built |
| Local tests | `pytest==8.3.5`, `httpx==0.28.1` | Installed from PyPI by `python3 -m pip install -r requirements-dev.txt` |

`requirements-dev.txt` includes both service requirement files. Docker Compose builds the API and poller on `python:3.12-slim`, uses `nginx:1.27-alpine` for the dashboard, and pulls `mongo:7` for storage. Docker Desktop supplies the Docker CLI and Compose; Compose obtains these images from Docker Hub during the first build/start.

## Setup

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env` and replace the `ADMIN_TOKEN` and `SENSOR_TOKEN` example values with different random secrets of at least 24 characters. For example, generate each value with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(36))'
```

Keep `.env` private. Install and start the native macOS sensor and the host control agent, then build and start the containers:

```sh
./scripts/install-sensor.sh
./scripts/install-control.sh
docker compose up -d --build
```

The script registers the host sensor with `launchd`; it runs in the background and can read macOS host counters that containers cannot access. Docker Compose exposes the dashboard only on `127.0.0.1`. The sensor listens on port `8765` by default so Docker Desktop can reach it and requires `SENSOR_TOKEN`.

The control agent (`host_control.py`, also a `launchd` service) lets the dashboard's **Configuration** form restart the Compose services. It listens on `127.0.0.1:8766` (`CONTROL_PORT`) and only accepts requests carrying `ADMIN_TOKEN`. It is optional: without it everything works except the **Restart services** button.

## Configuration

Open **Configuration** in the dashboard header to change the polling frequency, retention period, and external dashboard port, and to restart the services. Saving requires the administrator token (`ADMIN_TOKEN`). A changed port takes effect only after **Restart services**, which updates `DASHBOARD_PORT` in `.env` and recreates the containers. See the [user manual](docs/user-manual.md#configuration) for details.

For detailed usage and troubleshooting, see the [user manual](docs/user-manual.md). The component map, data flow, interfaces, and operational constraints are in the [design document](docs/design.md).

## Common operations

```sh
docker compose ps
docker compose logs --tail=100 api poller dashboard mongo
.venv/bin/python -m pytest -q
docker compose restart api poller dashboard
docker compose down       # stops containers; preserves MongoDB data
docker compose down -v    # also deletes the MongoDB data volume
```

To stop the host sensor or the control agent:

```sh
launchctl bootout "gui/$(id -u)/com.telemetry-observatory.sensor"
launchctl bootout "gui/$(id -u)/com.telemetry-observatory.control"
```

Re-run `./scripts/install-sensor.sh` (and `./scripts/install-control.sh` when `CONTROL_PORT` changes) after changing `.env` or moving the project directory. MongoDB removes expired samples asynchronously according to the configured retention policy. Back up the Compose MongoDB volume before deleting it.

After updating the application code, run `./scripts/install-sensor.sh` to restart the native sensor and `docker compose up -d --build` to rebuild and restart the containers.

## Data collection notes

The sensor uses `psutil` to collect CPU, physical and swap memory, disk capacity and I/O counters, network counters, battery state, and process information. It reads GPU device utilization from macOS IORegistry using `ioreg`, without administrator privileges. The dashboard displays CPU and GPU utilization, physical memory, disk read/write rates, network download/upload rates, swap use, mounted-volume usage, resource history, and the twelve processes using the most memory or CPU. GPU utilization is unavailable on systems that do not expose the counter; macOS may deny access to some processes or volumes, which are skipped. The dashboard reports this Mac only and does not aggregate remote hosts.

### Local models panel

The **Are we running on local AI?** panel shows whether work is running on local models. The sensor asks Ollama (`OLLAMA_URL`, default `http://127.0.0.1:11434`) for its version and loaded models via `/api/version` and `/api/ps`, including how much of each model sits in GPU memory. It also lists local-runtime processes (`ollama`, `llama-server`, LM Studio, MLX) and summarizes the last 24 hours of the token-telemetry JSONL log (`TOKEN_TELEMETRY_PATH`, default `~/.agents/data/token-telemetry/telemetry.jsonl`) into local vs remote calls and tokens. The dashboard classifies each sample as *active* (model loaded and GPU ≥ 15% or a runtime process ≥ 20% CPU), *loaded/idle*, *ready* or *offline*, draws an activity strip for recent samples, and writes plain-language observations such as CPU spill-over of model weights or remote-only usage. If Ollama is unreachable or the log is missing, the panel says so instead of showing zeros; calls logged without a token count are reported as unmeasured, so totals are marked partial. When the latest sample is stale, the verdict changes to *Status unknown* and present-tense observations are hidden. GPU load is device-wide, so the dashboard presents it as a hint of local inference, not proof. To use non-default sources with the launchd sensor, set `OLLAMA_URL` and/or `TOKEN_TELEMETRY_PATH` in `.env` before running `scripts/install-sensor.sh`.

