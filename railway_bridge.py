#!/usr/bin/env python3
"""railway-bridge - Railway's service metrics as Prometheus metrics.

Polls Railway's public GraphQL API for one service and serves what it learns
at /prometheus for HarbourMaster's Prometheus to scrape. Standard library only.

Configuration comes from the environment:

    RAILWAY_TOKEN           a project token for the service's environment (required)
    RAILWAY_PROJECT_ID      the project
    RAILWAY_ENVIRONMENT_ID  the environment, normally production
    RAILWAY_SERVICE_ID      the service
    SERVICE_LABEL           the `service` label on every metric (default: service)
    POLL_SECONDS            how often to poll (default 300)
    BRIDGE_PORT             port to listen on (default 8098)

Railway has no read-only token: a project token can also deploy and read the
environment's unsealed variables. Keep it on the board only, mode 600, and seal
the service's secrets in Railway so the token cannot read them.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_URL = "https://backboard.railway.com/graphql/v2"
TOKEN = os.environ.get("RAILWAY_TOKEN", "")
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
SERVICE_LABEL = os.environ.get("SERVICE_LABEL", "service")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8098"))

STEP = 60           # HTTP counts are read per minute
LAG = 180           # Railway's newest minutes are still filling in; leave them
SUMMARY_SECONDS = 3600
CLASSES = ("2xx", "3xx", "4xx", "5xx", "other")
QUANTILES = ("p50", "p90", "p95", "p99")
RESOURCES = {       # Railway measurement -> Prometheus name
    "CPU_USAGE": "railway_cpu_usage_vcpu",
    "CPU_LIMIT": "railway_cpu_limit_vcpu",
    "MEMORY_USAGE_GB": "railway_memory_usage_gb",
    "MEMORY_LIMIT_GB": "railway_memory_limit_gb",
    "DISK_USAGE_GB": "railway_disk_usage_gb",
    "NETWORK_RX_GB": "railway_network_rx_gb",
    "NETWORK_TX_GB": "railway_network_tx_gb",
}
VERSION = re.compile(r"\bv\d+\.\d+\.\d+[^\s:]*")
MERGE_HEADER = re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)")


# ---- pure helpers, covered by tests ------------------------------------------

def status_class(code) -> str:
    """Fold an HTTP status code into 2xx/3xx/4xx/5xx, anything else is 'other'."""
    if isinstance(code, int) and 200 <= code < 600:
        return f"{code // 100}xx"
    return "other"


def parse_deploy_message(message) -> tuple[str, str]:
    """(version, change) from a deploy's commit message.

    Written for a service that deploys from GitHub merge commits whose body
    names the version: `v1.4.2: what changed`, `v1.4.1 hotfix: what changed`,
    or `Trigger deploy: v1.3.0`. The version is taken from wherever it sits
    in the line and the rest becomes the change. A merge with no version keeps
    its body, or failing that the branch it merged.
    """
    if not message:
        return "", ""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    merge = MERGE_HEADER.match(lines[0])
    body = lines[1:] if merge else lines
    for line in body:
        match = VERSION.search(line)
        if match:
            rest = (line[:match.start()] + line[match.end():]).strip(" :-")
            return match.group(0), rest[:1].upper() + rest[1:]
    if body:
        return "", body[0]
    return "", merge.group(1)


def latest(values: list[dict]):
    """The newest sample's value, or None."""
    return max(values, key=lambda v: v["ts"])["value"] if values else None


def peak(values: list[dict]):
    """The largest sample's value, or None."""
    return max(v["value"] for v in values) if values else None


class RequestCounter:
    """Running request totals per status class, built from Railway's per-minute counts.

    Each poll re-reads an overlapping window, so every status code keeps the
    timestamp of the last bucket it added, and only completed buckets (older
    than `lag`) are taken. Nothing before `start` is counted, so a restart does
    not replay the look-back window as a burst of traffic.
    """

    def __init__(self, step: int = STEP, lag: int = LAG, start: int | None = None) -> None:
        self.step, self.lag = step, lag
        self.start = start if start is not None else 0
        self.totals = dict.fromkeys(CLASSES, 0)
        self._seen: dict[object, int] = {}

    def add(self, answer: list[dict], now: int) -> None:
        cutoff = now - self.lag
        for group in answer:
            code = group.get("statusCode")
            seen = self._seen.get(code, self.start - 1)
            newest = seen
            for sample in group.get("samples") or []:
                ts = sample["ts"]
                if seen < ts and ts >= self.start and ts + self.step <= cutoff:
                    self.totals[status_class(code)] += int(sample["value"])
                    newest = max(newest, ts)
            self._seen[code] = newest


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict) -> str:
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())) + "}"


def to_prometheus(state: dict) -> str:
    """Prometheus text exposition for one poll's state."""
    service = {"service": state["service"]}
    out: list[str] = []

    def metric(name: str, kind: str, help_text: str, rows: list[tuple[dict, object]]) -> None:
        rows = [(labels, value) for labels, value in rows if value is not None]
        if not rows:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{_labels(dict(service, **labels))} {value}" for labels, value in rows)

    metric("railway_bridge_up", "gauge", "1 when the last poll of Railway succeeded.", [({}, state.get("up", 0))])
    metric("railway_bridge_last_success_timestamp_seconds", "gauge", "When Railway was last polled successfully.",
           [({}, state.get("last_success"))])
    metric("railway_http_requests_total", "counter",
           "HTTP requests served, by status class, counted from Railway's per-minute figures since the bridge started.",
           [({"class": c}, n) for c, n in (state.get("requests") or {}).items()])
    metric("railway_http_latency_ms", "gauge", "HTTP response time over the last poll window, by percentile.",
           [({"quantile": q}, (state.get("latency") or {}).get(q)) for q in QUANTILES])
    for measurement, name in RESOURCES.items():
        metric(name, "gauge", f"Railway {measurement}, newest sample.",
               [({}, (state.get("resources") or {}).get(measurement))])
    summary = state.get("summary") or {}
    metric("railway_requests_7d", "gauge", "Requests over the last 7 days, from Railway.",
           [({}, summary.get("requests_7d"))])
    metric("railway_errors_5xx_7d", "gauge", "5xx responses over the last 7 days, from Railway.",
           [({}, summary.get("errors_5xx_7d"))])
    metric("railway_memory_peak_7d_gb", "gauge", "Peak memory over the last 7 days, from Railway.",
           [({}, summary.get("memory_peak_7d_gb"))])
    metric("railway_deployment_created_timestamp_seconds", "gauge",
           "Recent deployments: value is when each was created; labels say what it was.",
           [({"id": d["id"], "status": d["status"], "version": d["version"], "change": d["change"]}, d["created"])
            for d in state.get("deployments") or []])
    return "\n".join(out) + "\n"


# ---- Railway --------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


class Railway:
    """A thin client for Railway's GraphQL API with a project token."""

    def __init__(self, token: str) -> None:
        self.token = token

    def query(self, text: str, variables: dict) -> dict:
        request = urllib.request.Request(
            API_URL, data=json.dumps({"query": text, "variables": variables}).encode(), method="POST",
            headers={"Project-Access-Token": self.token, "Content-Type": "application/json",
                     "User-Agent": "harbourmaster-railway-bridge"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
        if body.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"])[:300])
        return body["data"]

    def http_by_status(self, start: float, end: float, step: int) -> list[dict]:
        return self.query(
            """query($s: String!, $e: String!, $from: DateTime!, $to: DateTime!, $step: Int!) {
                 httpMetricsGroupedByStatus(serviceId: $s, environmentId: $e, startDate: $from,
                                            endDate: $to, stepSeconds: $step) {
                   statusCode samples { ts value } } }""",
            {"s": SERVICE_ID, "e": ENVIRONMENT_ID, "from": _iso(start), "to": _iso(end), "step": step},
        )["httpMetricsGroupedByStatus"]

    def latency(self, start: float, end: float) -> dict:
        samples = self.query(
            """query($s: String!, $e: String!, $from: DateTime!, $to: DateTime!, $step: Int!) {
                 httpDurationMetrics(serviceId: $s, environmentId: $e, startDate: $from,
                                     endDate: $to, stepSeconds: $step) {
                   samples { ts p50 p90 p95 p99 } } }""",
            {"s": SERVICE_ID, "e": ENVIRONMENT_ID, "from": _iso(start), "to": _iso(end), "step": int(end - start)},
        )["httpDurationMetrics"]["samples"]
        newest = max(samples, key=lambda s: s["ts"]) if samples else {}
        return {q: newest.get(q) for q in QUANTILES}

    def resources(self, start: float, end: float, sample_rate: int) -> dict[str, list[dict]]:
        results = self.query(
            """query($p: String!, $s: String!, $e: String!, $from: DateTime!, $to: DateTime!,
                     $m: [MetricMeasurement!]!, $rate: Int!) {
                 metrics(projectId: $p, serviceId: $s, environmentId: $e, startDate: $from, endDate: $to,
                         measurements: $m, sampleRateSeconds: $rate) {
                   measurement values { ts value } } }""",
            {"p": PROJECT_ID, "s": SERVICE_ID, "e": ENVIRONMENT_ID, "from": _iso(start), "to": _iso(end),
             "m": list(RESOURCES), "rate": sample_rate},
        )["metrics"]
        return {r["measurement"]: r["values"] for r in results}

    def deployments(self, count: int = 20) -> list[dict]:
        edges = self.query(
            """query($p: String!, $s: String!, $e: String!, $n: Int!) {
                 deployments(first: $n, input: {projectId: $p, serviceId: $s, environmentId: $e}) {
                   edges { node { id status createdAt meta } } } }""",
            {"p": PROJECT_ID, "s": SERVICE_ID, "e": ENVIRONMENT_ID, "n": count},
        )["deployments"]["edges"]
        rows = []
        for edge in edges:
            node = edge["node"]
            version, change = parse_deploy_message((node.get("meta") or {}).get("commitMessage"))
            rows.append({"id": node["id"], "status": node["status"], "created": _epoch(node["createdAt"]),
                         "version": version, "change": change[:120]})
        return rows


# ---- the poller and the server ------------------------------------------------

class Poller:
    """Polls Railway on a timer and keeps the latest state for the HTTP handler."""

    def __init__(self, railway: Railway) -> None:
        self.railway = railway
        self.counter = RequestCounter(start=int(time.time()) - LAG)
        self.lock = threading.Lock()
        self.state = {"service": SERVICE_LABEL, "up": 0, "last_success": None,
                      "requests": dict(self.counter.totals)}
        self.last_summary = 0.0
        self.last_error = ""

    def poll_once(self) -> None:
        now = time.time()
        update = {
            "requests": None,
            "latency": self.railway.latency(now - POLL_SECONDS, now),
            "resources": {m: latest(v) for m, v in self.railway.resources(now - 900, now, 60).items()},
            "deployments": self.railway.deployments(),
        }
        self.counter.add(self.railway.http_by_status(now - POLL_SECONDS - LAG - 2 * STEP, now, STEP), int(now))
        update["requests"] = dict(self.counter.totals)
        if now - self.last_summary >= SUMMARY_SECONDS:
            update["summary"] = self._summary(now)
            self.last_summary = now
        with self.lock:
            self.state.update(update, up=1, last_success=int(now))
            self.last_error = ""

    def _summary(self, now: float) -> dict:
        week = now - 7 * 86400
        by_status = self.railway.http_by_status(week, now, 3600)
        total = sum(int(s["value"]) for g in by_status for s in g["samples"] or [])
        errors = sum(int(s["value"]) for g in by_status if status_class(g["statusCode"]) == "5xx"
                     for s in g["samples"] or [])
        memory = self.railway.resources(week, now, 3600).get("MEMORY_USAGE_GB", [])
        return {"requests_7d": total, "errors_5xx_7d": errors, "memory_peak_7d_gb": peak(memory)}

    def run(self) -> None:
        while True:
            try:
                self.poll_once()
            # Anything, not a list: urlopen lets a dropped connection through as
            # RemoteDisconnected, and one uncaught error would end polling for good.
            except Exception as error:  # noqa: BLE001
                with self.lock:
                    self.state["up"] = 0
                    self.last_error = str(error)[:300]
                print(f"poll failed: {self.last_error}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)

    def snapshot(self) -> tuple[dict, str]:
        with self.lock:
            return dict(self.state), self.last_error


POLLER: Poller | None = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, content_type: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        state, error = POLLER.snapshot()
        if self.path == "/prometheus":
            self._send(200, to_prometheus(state), "text/plain; version=0.0.4")
        elif self.path == "/health":
            age = time.time() - state["last_success"] if state.get("last_success") else None
            healthy = age is not None and age < 3 * POLL_SECONDS
            body = {"status": "ok" if healthy else "stale", "service": state["service"],
                    "last_success_age_seconds": None if age is None else int(age), "error": error or None}
            self._send(200 if healthy else 503, json.dumps(body), "application/json")
        else:
            self._send(404, json.dumps({"error": "try /prometheus or /health"}), "application/json")

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    global POLLER
    missing = [n for n in ("RAILWAY_TOKEN", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID")
               if not os.environ.get(n)]
    if missing:
        sys.exit(f"missing configuration: {', '.join(missing)}")
    POLLER = Poller(Railway(TOKEN))
    threading.Thread(target=POLLER.run, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", BRIDGE_PORT), Handler)
    print(f"railway-bridge for {SERVICE_LABEL} on 0.0.0.0:{BRIDGE_PORT}, polling every {POLL_SECONDS}s", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
