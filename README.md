# railway-bridge

Railway's service metrics, served for Prometheus to scrape.

Railway shows you request counts, latency, CPU and memory in its dashboard, but it doesn't export any of it. There's no `/metrics` endpoint to point Prometheus at. So if the rest of your monitoring lives in Grafana, the one service on Railway is a blank spot.

This fills it. It polls Railway's public GraphQL API every five minutes with a project token and serves what it learns at `/prometheus`. One file, standard library only, no dependencies.

## What you get

This is the exporter's own output for the state the tests use, trimmed:

```
railway_bridge_up{service="web"} 1
railway_http_requests_total{class="2xx",service="web"} 10
railway_http_requests_total{class="5xx",service="web"} 0
railway_http_latency_ms{quantile="p95",service="web"} 450.5
railway_memory_usage_gb{service="web"} 0.7
railway_memory_limit_gb{service="web"} 2.0
railway_requests_7d{service="web"} 12480
railway_deployment_created_timestamp_seconds{change="Reports",id="d2",service="web",status="REMOVED",version="v1.4.0"} 1791500000
...
```

The full set:

- **Requests by status class** (`2xx` to `5xx`), as a real counter, so `increase()` and `rate()` work.
- **Latency** at p50, p90, p95 and p99.
- **CPU, memory, disk and network**, with the limits next to the usage.
- **7-day totals**, refreshed hourly: requests, 5xx errors, peak memory.
- **The last 20 deploys**, with the version and the change pulled out of the merge commit message, so a Grafana annotation can say what went out and not just that something did.
- **`railway_bridge_up`** and a `/health` endpoint that goes `503` once Railway hasn't been read for three polls, so an expired token doesn't just look like a quiet week.

## The counter is the fiddly part

Railway hands back requests as per-minute buckets, and the newest few minutes are still filling in. If you add up whatever comes back on each poll, you double-count the overlap and count half-full minutes. If you start from zero on every restart, Prometheus sees a reset; if you count the whole look-back window on a restart, it sees a spike.

So `RequestCounter` remembers the last bucket it took per status code, only takes buckets older than a three-minute lag, and ignores anything from before it started. The tests cover each of those cases.

## Running it

```sh
install -m 600 railway-bridge.env.example railway-bridge.env   # then fill in the token and IDs
docker run -d --name railway-bridge --restart unless-stopped -p 127.0.0.1:8098:8098 \
  --env-file railway-bridge.env -v "$PWD/railway_bridge.py:/app/railway_bridge.py:ro" \
  python:3.12.15-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 \
  python /app/railway_bridge.py
```

Or use the included Compose file, which pins the runtime and applies a read-only filesystem, dropped capabilities and a container healthcheck. It refuses to start until you name the address to listen on:

```sh
install -m 600 railway-bridge.env.example railway-bridge.env   # then fill in the token and IDs
RAILWAY_BRIDGE_BIND_IP=127.0.0.1 docker compose up -d
```

Both examples bind the exporter to loopback because its endpoints are unauthenticated. If Prometheus runs on another host, publish it only on a private address protected by the host firewall, never `0.0.0.0`. The Compose deployment fixes the application port at 8098 so its mapping and healthcheck stay consistent.

Or without Docker: `set -a; . ./railway-bridge.env; set +a; python3 railway_bridge.py`.

The IDs are in the Railway dashboard URL when you have the service open: `/project/<project>/service/<service>?environmentId=<environment>`. The token is a project token from the project's settings.

Then scrape it:

```yaml
scrape_configs:
  - job_name: railway
    metrics_path: /prometheus
    static_configs:
      - targets: ["localhost:8098"]
```

## Deploying it to another host

`deploy.sh` is how I put it on a Docker host in my homelab. It is opinionated, and you'll want to read it and `remote/install.sh` before using it. It needs SSH with sudo on the target, Docker Compose, systemd and `flock` there, and a local settings file, by default `~/.config/railway-bridge/deploy.env`, mode 600. `render.py` lists what goes in it: the SSH target, optionally the host name the target must answer with, the private address and interface to bind to, and the Railway token and IDs.

Run the tests first, as their own step. `deploy.sh` never runs them: a deploy script that runs the tests, while the tests run the deploy script, recurses until the machine runs out of memory.

```sh
python3 -m unittest discover -s tests
./deploy.sh            # install-only: stage, check, pull and publish; nothing restarts
./deploy.sh --start    # the same, then recreate the container and wait for it to be healthy
```

What it does on the host:

- The settings are rendered locally into a private temporary directory with a SHA-256 manifest, and travel over SSH's stdin, never in a command line. The installer is not uploaded; it is sent inside the SSH command and run with `sudo /bin/bash -s`.
- As root it copies the stage into a new root-only release under `/opt/railway-bridge-releases`, checks every file against the manifest, checks the env files hold exactly what the container and the bind address need, and has Compose confirm the only service and the pinned image digest.
- It installs and enables `railway-bridge-reconcile.service`, which after a reboot waits for the private address to exist before recreating the container, so Docker never binds to an address that isn't there yet. Check it with `systemctl status railway-bridge-reconcile.service`.
- It points `/opt/railway-bridge` at the new release with one atomic rename.

An install-only run changes nothing that is serving. With `--start`, a release that does not come up healthy is a rollback: `/opt/railway-bridge` is pointed back at the previous release and that is recreated. On a first install the failed project is taken down instead. The failed release is kept as `<name>.failed`. A failure or Ctrl-C before that point puts the link, the container and the systemd unit back as they were. Every release, `.failed` ones included, keeps its own copy of the token; list them with `sudo ls -l /opt/railway-bridge-releases` and remove the ones you no longer need, and a `.failed` one once you have looked at why it failed.

The remote side expects the SSH user's login shell to be POSIX (bash, dash or similar), and the host needs `python3` for the installer's port check.

Then verify from a machine that can reach the bind address:

```sh
curl -s http://<bind address>:8098/health
curl -s http://<bind address>:8098/prometheus | grep -c '^railway_'
```

### Moving it between hosts

When the bridge moves from one host to another, I treat each step as a migration gate and do not start the next until the last one is verified:

1. Record the before-state on the old host: health, the metric count, and a Grafana screenshot.
2. Run the install-only deploy on the new host, then `--start`, and verify health and metrics there.
3. Point Prometheus's scrape target and the Uptime Kuma monitor at the new address, and confirm both see it.
4. Reboot the new host and confirm the container comes back on its own, through the reconcile service.
5. Only then stop the source container on the old host. Keep it and its files for rollback until the new one has run cleanly for a while; starting the old one again is the way back.

## About the token

Railway has no read-only token. A project token can also deploy, and read any of the environment's variables that aren't sealed. Keep it somewhere only the bridge can read, and seal your service's secrets in Railway so the token can't see them.

`/prometheus` and `/health` have no authentication, like most exporters. They show your request counts and recent deploy messages, so keep the port on your LAN or behind your firewall, not on the internet.

## Tests

```sh
python3 -m unittest discover -s tests
```

No network needed.

Built by Wilhelm Maritz.
