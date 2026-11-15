#!/usr/bin/env python3
"""Checks that Prometheus reports the injected faults as configured.

Runs every fault side by side in the metric-check namespace (manifest.yaml),
lets them serve a client for a few minutes, and compares three views of each:
  configured  the env var (matrix.toml)
  observed    the client's log (status, time) and the app itself (delay_ms in
              each response, leaked memory from /stats), independent of Prometheus
  Prometheus  the queries of the analysis templates, evaluated every 20 s as an
              AnalysisRun would, and exact counts over the whole window
Writes results/<time>.json and prints a summary. Deletes the namespace after.

  check.py [--minutes 5] [--keep]
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "trial-runner"))
from trial import config, measure  # noqa: E402
from trial.prom import Prometheus  # noqa: E402

CTX = "k3s-dev"
NS = "metric-check"
FAULTS = ["f0", "f1", "f2", "f3", "f4", "f5"]
WARMUP_S = 75  # the 1m rate windows must lie entirely after start-up
STEP_S = 20  # interval of the analysis checks
MIB = 1048576


def kubectl(*args, **kw) -> str:
    return subprocess.run(["kubectl", "--context", CTX, *args], capture_output=True, text=True,
                          check=True, **kw).stdout


def p95(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return xs[0] if xs else None
    return statistics.quantiles(xs, n=20, method="inclusive")[18]


def spread(xs: list) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {}
    return {"n": len(xs), "min": min(xs), "median": statistics.median(xs), "max": max(xs)}


def expected(cfg, f: str) -> dict:
    env = cfg.release_env(f)
    jitter, lat = float(env["LATENCY_JITTER_MS"]), float(env["LATENCY_MS"])
    return {
        "error_share": float(env["ERROR_RATE"]) / 100,
        # Fixed delay plus the 95th percentile of uniform(0, jitter); the app's own
        # processing (a few ms) comes on top.
        "latency_p95_ms": lat + 0.95 * jitter,
        "memory_mib_s": float(env["LEAK_MB_PER_MIN"]) / 60,  # the app leaks MiB (2**20)
    }


def stats_leaked(f: str) -> tuple[float, float]:
    out = kubectl("-n", NS, "exec", "deploy/client", "-c", "curl", "--",
                  "curl", "-s", f"http://mc-{f}/stats")
    return time.time(), json.loads(out)["leaked_mb"]


def client_log(w0: float, w1: float) -> dict[str, list[tuple]]:
    rows = {f: [] for f in FAULTS}
    for line in kubectl("-n", NS, "logs", "deploy/client", "-c", "curl", "--since=30m").splitlines():
        p = line.split()
        if len(p) == 5 and p[1] in rows and w0 <= int(p[0]) <= w1:
            rows[p[1]].append((int(p[0]), int(p[2]), float(p[3]) * 1000, None if p[4] == "NA" else int(p[4])))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--keep", action="store_true", help="don't delete the namespace")
    args = ap.parse_args()

    cfg = config.load(HERE.parent / "trial-runner" / "matrix.toml")
    prom = Prometheus(cfg.prometheus, cfg.ca_file)
    image = kubectl("-n", "demo", "get", "rollout", "demo", "-o",
                    "jsonpath={.spec.template.spec.containers[0].image}")
    manifest = (HERE / "manifest.yaml").read_text().replace("IMAGE", image)
    print(f"image {image}")
    try:
        kubectl("apply", "-f", "-", input=manifest)
        for d in [f"mc-{f}" for f in FAULTS] + ["client"]:
            kubectl("-n", NS, "rollout", "status", f"deploy/{d}", "--timeout=300s")
        skew = time.time() - prom.server_time()
        w0 = time.time() + WARMUP_S
        w1 = w0 + args.minutes * 60
        print(f"all ready; window {datetime.fromtimestamp(w0):%H:%M:%S}-{datetime.fromtimestamp(w1):%H:%M:%S}")
        time.sleep(WARMUP_S)
        leak0 = stats_leaked("f5")
        time.sleep(w1 - time.time())
        leak1 = stats_leaked("f5")
        time.sleep(measure.SCRAPE_LAG_S)  # let the last scrape land
        log = client_log(w0, w1)
    finally:
        if not args.keep:
            subprocess.run(["kubectl", "--context", CTX, "delete", "namespace", NS, "--wait=false"],
                           capture_output=True)

    instants = [t for t in range(int(w0 + 60), int(w1) + 1, STEP_S)]  # windows inside [w0, w1]
    out = {"image": image, "window": [w0, w1], "skew_s": skew, "faults": {}}
    for f in FAULTS:
        rows = log[f]
        dest = f'reporter="destination", destination_workload_namespace="{NS}", destination_workload="mc-{f}"'
        src = f'reporter="source", source_workload="client", destination_workload="mc-{f}"'
        r = {"expected": expected(cfg, f)}
        r["client"] = {
            "requests": len(rows),
            "error_share": sum(c == 500 for _, c, _, _ in rows) / len(rows) if rows else None,
            "latency_p95_ms": p95([ms for _, _, ms, _ in rows]),
            "app_delay_p95_ms": p95([d for *_, d in rows if d is not None]),
        }
        exact = measure._requests(prom, dest, w0 - skew, w1 - skew)
        r["prometheus_exact"] = {**exact, "error_share": exact["5xx"] / exact["total"] if exact["total"] else None}

        def every(expr):
            return [prom.scalar(expr, at=t - skew) if prom.query(expr, at=t - skew)["result"] else None
                    for t in instants]

        def err_share(sel):
            return (f'(sum(rate(istio_requests_total{{{sel}, response_code=~"5.*"}}[1m])) or vector(0))'
                    f' / sum(rate(istio_requests_total{{{sel}}}[1m]))')

        def p95_q(sel):
            return (f'histogram_quantile(0.95, sum by (le) (rate('
                    f'istio_request_duration_milliseconds_bucket{{{sel}}}[1m])))')

        r["per_check"] = {
            # What one AnalysisRun measurement would read, every 20 s.
            "error_share_source": spread(every(err_share(src))),
            "error_share_destination": spread(every(err_share(dest))),
            "latency_p95_ms_source": spread(every(p95_q(src))),
            "memory_mib_s": spread(every(
                f'max(deriv(container_memory_working_set_bytes{{namespace="{NS}", container="app", '
                f'pod=~"mc-{f}-.*"}}[1m])) / {MIB}')),
            # The client's own p95 over the same 1-minute windows.
            "client_latency_p95_ms": spread([p95([ms for ts, _, ms, _ in rows if t - 60 < ts <= t])
                                             for t in instants]),
        }
        if f == "f5":
            r["app_leak_mib_s"] = (leak1[1] - leak0[1]) / (leak1[0] - leak0[0])
        out["faults"][f] = r

    res = HERE / "results"
    res.mkdir(exist_ok=True)
    path = res / f"{datetime.now():%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1))
    print(json.dumps(out["faults"], indent=1))
    print(f"saved {path.relative_to(HERE)}")


if __name__ == "__main__":
    main()
