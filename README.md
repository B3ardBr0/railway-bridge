# railway-bridge

Railway's service metrics, served for Prometheus to scrape.

Railway shows you request counts, latency, CPU and memory in its dashboard, but it doesn't export any of it. There's no `/metrics` endpoint to point Prometheus at. So if the rest of your monitoring lives in Grafana, the one service on Railway is a blank spot.

This fills it. It polls Railway's public GraphQL API every five minutes with a project token and serves what it learns at `/prometheus`. One file, standard library only, no dependencies.

## What you get

```
$ curl -s localhost:8098/prometheus
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

That's trimmed. The full set:

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
cp railway-bridge.env.example railway-bridge.env   # fill in the token and IDs
docker run -d --name railway-bridge --restart unless-stopped -p 8098:8098 \
  --env-file railway-bridge.env -v "$PWD/railway_bridge.py:/app/railway_bridge.py:ro" \
  python:3.12-alpine python /app/railway_bridge.py
```

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

`deploy.sh` is how I put it on my homelab board: it reads the settings from a local secrets file, copies them over ssh in a file only the owner can read, and recreates the container. You'll want to change the paths if you use it.

## About the token

Railway has no read-only token. A project token can also deploy, and read any of the environment's variables that aren't sealed. Keep it somewhere only the bridge can read, and seal your service's secrets in Railway so the token can't see them.

`/prometheus` and `/health` have no authentication, like most exporters. They show your request counts and recent deploy messages, so keep the port on your LAN or behind your firewall, not on the internet.

## Tests

```sh
python3 -m unittest discover -s tests
```

No network needed.

Built by Wilhelm Maritz.
