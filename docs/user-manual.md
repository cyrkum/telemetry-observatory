# User manual

Telemetry Observatory presents resource and process information from one macOS host. The host sensor runs natively on macOS; the API, poller, MongoDB, and dashboard run in Docker containers.

## Start and open the dashboard

Complete the setup in the [README](../README.md), including creating `.env`, installing the host sensor and control agent, and starting Docker Compose. Open <http://127.0.0.1:8080>.

Check whether the services are running with:

```sh
docker compose ps
```

## Read the dashboard

- **CPU utilization** shows current CPU use.
- **GPU Utilization** shows the first available GPU device utilization percentage reported by macOS, next to CPU. A dash means the counter is unavailable or the sample predates GPU collection; check `audits/sensor.jsonl` for `gpu.read` errors.
- **Memory used** shows used physical memory and its percentage of total physical memory.
- **Disk read** and **Disk write** show rates calculated from the difference between the two newest cumulative I/O samples. Rates are shown as unavailable until two samples exist.
- **Network down** and **Network up** show bytes received and sent per second across all network interfaces, calculated the same way from cumulative network counters.
- **Resource history** plots CPU, GPU and memory percentages. Hover over a plotted line to see a tooltip with that resource's percentage and the timestamp of the nearest sample. Gaps in GPU history indicate unavailable readings or samples collected before GPU monitoring was added; no tooltip is shown for unavailable readings. **Live** shows the newest 120 samples and refreshes every 5 seconds. **1 h**, **24 h** and **7 d** show averages over that period, at most 360 points, and refresh every minute. GPU averages exclude unavailable readings. A period can only show samples that are still within the retention policy.
- **Storage** lists the main volumes: those of at least 10 GB, excluding macOS system volumes other than `/System/Volumes/Data` and `/private/…` mounts. Select **Show all** to include mounted disk images, small external drives and system volumes.
- An **Attention** box appears under the header when CPU has averaged 90% or more over the last 5 minutes, memory use is 90% or more, a volume of at least 10 GB is 90% or more full, or the battery is at 15% or less while unplugged. Small mounted disk images (such as app installers) and macOS system volumes other than `/System/Volumes/Data` are ignored, because they are normally full. The box disappears once the condition clears.
- **Swap used** shows how much disk macOS is using as extra memory. Steady growth means the Mac is short of physical memory.
- **Are we running on local AI?** (above the applications table) shows whether Ollama is reachable, which models are loaded and how much of each is in GPU memory, an activity strip of recent samples, the 24-hour local vs remote token split from token-telemetry, and automatic observations. If the sensor stops reporting, the verdict shows *Status unknown* until a fresh sample arrives. Calls without a token count make the totals *partial*. Optional `OLLAMA_URL` and `TOKEN_TELEMETRY_PATH` values in `.env` are passed to the sensor by `scripts/install-sensor.sh`.
- **Applications by memory** lists up to twelve processes. Select the **Memory** or **CPU** column header to order them by that value. Per-process CPU can exceed 100% because it is summed across cores, as in Activity Monitor, and it reads 0% on the first sample after the sensor starts.
- The dashboard requests updated data every five seconds.

The header status describes the latest sample/API connection:

- **Live telemetry** means the latest sample is within the freshness window.
- **Stale sample** means the API is reachable, but the latest sample is older than the freshness window. The sensor or poller may not be delivering new samples.
- **Disconnected** means the dashboard could not retrieve samples from the API.

Some process or volume information may be absent because macOS denied access. A process CPU reading can be zero on its first observation.

## Configuration

Choose **Configuration** in the dashboard header to open the configuration form:

- **Poll every**: 2 to 3600 seconds. The default is 10 seconds.
- **Retain for**: 1 to 365 days. The default is 7 days.
- **External port**: host port for the dashboard (1024 to 65535). Changing it shows a warning because a restart of services is required.
- **Administrator token**: the `ADMIN_TOKEN` value from `.env`; required to save or restart. The browser does not persist it.

**Save configuration** stores the values. The poller reads the updated frequency at the start of its next cycle, and retention changes recalculate expiry times for existing samples (MongoDB's TTL monitor deletes asynchronously).

**Restart services** saves the form, writes `DASHBOARD_PORT` to `.env`, and runs `docker compose up -d --force-recreate` through the host control agent. Install the agent once (and after moving the project) with `./scripts/install-control.sh`; it listens on `127.0.0.1:${CONTROL_PORT:-8766}` and accepts only requests carrying the administrator token. If it is not running, the restart fails with "Restart agent is unavailable". After a port change the page redirects to the new port.

## Stop, restart, and remove data

Restart application containers:

```sh
docker compose restart api poller dashboard
```

Stop containers while preserving saved samples:

```sh
docker compose down
```

Stop containers and permanently delete the MongoDB volume and saved samples:

```sh
docker compose down -v
```

Stop the macOS host sensor separately:

```sh
launchctl bootout "gui/$(id -u)/com.telemetry-observatory.sensor"
```

Stop the host control agent with `launchctl bootout "gui/$(id -u)/com.telemetry-observatory.control"`.

Run `./scripts/install-sensor.sh` again to install/start the sensor, including after changing `.env` or moving the project directory; do the same with `./scripts/install-control.sh` for the control agent. Back up the `mongo_data` Compose volume before removing it if the samples need to be retained.

## Troubleshooting

1. **Dashboard does not open:** Check `docker compose ps` and confirm Docker Desktop is running. The dashboard port can be changed in **Configuration** (then **Restart services**) or with `DASHBOARD_PORT` in `.env`; the default is 8080. After a port change, open the new port.
2. **Disconnected status:** Inspect API and dashboard logs with `docker compose logs --tail=100 api dashboard`. The dashboard is served through Nginx, which proxies `/api/` requests to the API.
3. **Stale sample:** Check that the host sensor is installed and running, and inspect `audits/sensor.jsonl` and `audits/poller.jsonl`. Verify that `SENSOR_TOKEN` in `.env` is valid and that the sensor port is reachable from Docker Desktop.
4. **Services fail to start:** Check API and MongoDB startup output with `docker compose logs --tail=100 api mongo`. Verify `ADMIN_TOKEN` and `SENSOR_TOKEN` are each set to at least 24 characters in `.env`.
5. **Configuration save fails:** Confirm the administrator token matches `ADMIN_TOKEN` in `.env`; check `audits/api.jsonl` for denied authorization or validation requests.
6. **Restart services fails:** "Restart agent is unavailable" means the control agent is not reachable; run `./scripts/install-control.sh` and check `audits/control-console.log`. A "restart already in progress" error means a previous restart is still running; wait and retry. The agent needs `docker` on its `PATH`, which the installer captures from your current shell.

Audit files are written under `audits/`: `sensor.jsonl`, `poller.jsonl`, `api.jsonl`, `dashboard-access.log`, `dashboard-error.log`, and `mongo.log`. These logs can contain operational and request metadata; protect and rotate them appropriately.
