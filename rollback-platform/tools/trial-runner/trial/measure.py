"""Turns a timeline into the trial's measurements (docs/thesis-test-plan.md, section 5).

Times from the timeline are local; times inside Kubernetes objects are the
cluster's. clock_skew_s (cluster minus local) converts between them.
Prometheus is queried right away and the raw numbers are stored with the
result, because Prometheus only keeps 24 hours.
"""

from .observe import _ts, aborted, exposure, mirroring
from .prom import Prometheus

SLOW_MS = "500.0"  # nearest histogram bucket; Istio has none at 300 ms
SCRAPE_LAG_S = 30  # scrape interval + rate window catch-up after the last request


def _first(events, pred, after=None):
    for e in events:
        if (after is None or e["t"] >= after) and pred(e["snap"]):
            return e["t"]
    return None


def _round(x):
    return round(x, 1) if x is not None else None


def _caught_by(snap: dict, skew: float) -> tuple[str | None, float | None]:
    """The failing metric that finished first, and when (local time)."""
    failures = [
        (_ts(m["last_finished"]) - skew, f"{run['type']}/{name}")
        for run in snap["analysis"].values()
        for name, m in run["metrics"].items()
        if m["phase"] in ("Failed", "Error") and m["last_finished"]
    ]
    if not failures:
        return None, None
    t, name = min(failures)
    return name, t


def _times(tl: dict) -> dict:
    """The moments that every measurement is computed from (local time, or None)."""
    bad = tl["bad_hash"]
    # Before the start, the snapshot shows the previous release's runs (the
    # "current" hash is still the old one), so only look from the start on.
    ev = [e for e in tl["events"] if e["t"] >= tl["t_start"]]
    t = {"start": tl["t_start"], "end": tl["t_end"]}
    t["mirror_on"] = _first(ev, mirroring)
    t["mirror_off"] = _first(ev, lambda s: not mirroring(s), after=t["mirror_on"]) if t["mirror_on"] else None
    t["exposed"] = _first(ev, lambda s: exposure(s, bad) > 0)
    # Blue-green: the smoke Job starts with the pre-promotion run.
    t["pre"] = _first(ev, lambda s: any(a["type"] == "PrePromotion" for a in s["analysis"].values()))
    # First request of any kind to the new version.
    t["traffic"] = min((x for x in (t["mirror_on"], t["exposed"], t["pre"]) if x), default=None)
    t["abort"] = _first(ev, aborted)
    t["recover"] = (_first(ev, lambda s: exposure(s, bad) == 0 and not mirroring(s), after=t["abort"])
                    if t["abort"] else None)
    return t


def _detection(tl: dict, t: dict) -> tuple[str | None, float | None]:
    """Which check stopped the release, and when. None, None if it was promoted."""
    if not t["abort"]:
        return None, None
    last = tl["events"][-1]["snap"]
    name, t_detect = _caught_by(last, tl["clock_skew_s"])
    if name:
        return name, t_detect
    msg = last["rollout"]["message"] or ""
    if "exceeded its progressDeadlineSeconds" in msg:
        return "Experiment/progress-deadline", t["abort"]  # the experiment's own pods never got ready
    if "timed out progressing" in msg or "ProgressDeadline" in msg:
        return "progress-deadline", t["abort"]
    return f"other: {msg}", t["abort"]


def _exposure_stats(tl: dict) -> tuple[float, float]:
    """Peak percent of users on the new version, and for how many seconds any were."""
    ev, bad = tl["events"], tl["bad_hash"]
    peak = max(exposure(e["snap"], bad) for e in ev)
    ends = [e["t"] for e in ev[1:]] + [tl["t_end"]]
    seconds = sum(end - e["t"] for e, end in zip(ev, ends) if exposure(e["snap"], bad) > 0)
    return peak, seconds


def _trial_pod_minutes(tl: dict) -> float:
    """Pods created during the trial, from first sighting until they vanish or the trial ends."""
    ev = tl["events"]
    initial = set(ev[0]["snap"]["pods"])
    ends = [e["t"] for e in ev[1:]] + [tl["t_end"]]  # a snapshot holds until the next one
    seen: dict[str, list[float]] = {}
    for e, end in zip(ev, ends):
        for name in e["snap"]["pods"].keys() - initial:
            seen.setdefault(name, [e["t"], end])[1] = end
    return sum(b - a for a, b in seen.values()) / 60


def _requests(prom: Prometheus, selector: str, t0: float, t1: float) -> dict:
    """Total, 5xx and slow requests matching `selector` between t0 and t1.

    Exact counts: each counter's last value up to t1 minus its last value
    before t0 (a series that didn't exist yet counts from 0). increase() would
    extrapolate to the window edges and overcount short bursts (it read the
    smoke Job's 50 requests as 63).
    """
    rng = max(int(t1 - t0), 1)

    def count(metric, extra=""):
        def at(t, r):
            return prom.scalar(f"sum(last_over_time({metric}{{{selector}{extra}}}[{r}s]))", at=t)
        return at(t1, rng) - at(t0, 300)

    hist = "istio_request_duration_milliseconds_bucket"
    total = count("istio_requests_total")
    err = count("istio_requests_total", ', response_code=~"5.."')
    # Slow: from the histogram alone (its +Inf bucket is its own total).
    slow = count(hist, ', le="+Inf"') - count(hist, f', le="{SLOW_MS}"')
    return {"total": round(total), "5xx": round(err), "slow": max(round(slow), 0)}


def _traffic(tl: dict, t: dict, prom: Prometheus) -> dict:
    """Request counts from Prometheus: users on the new version, mirrored copies, smoke Job."""
    ns, bad = tl["namespace"], tl["bad_hash"]
    out = {"user": {"total": 0, "5xx": 0, "slow": 0}}
    if t["exposed"]:
        # Users: what the gateway sent to the new version while it was exposed,
        # read from the new pods' own sidecar by their hash, so it counts every
        # route to them (canary weight, or the stable Service after promotion).
        # The window starts after mirroring ended, so mirrored copies (also from
        # the gateway) aren't counted as users.
        w0 = max(t["exposed"] - SCRAPE_LAG_S / 2, t["mirror_off"] or 0)
        w1 = (t["recover"] or t["end"]) + SCRAPE_LAG_S
        pods = (f'reporter="destination", namespace="{ns}", rollouts_pod_template_hash="{bad}", '
                'source_workload="istio-ingressgateway"')
        user = _requests(prom, pods, w0, w1)
        if tl["kind"] == "canary":
            # The experiment's canary pods carry their own hash; count them through their Service.
            exp = (f'reporter="source", destination_service_namespace="{ns}", '
                   'destination_service_name="demo-exp-canary"')
            user = {k: v + _requests(prom, exp, w0, w1)[k] for k, v in user.items()}
        out["user"] = user | {"window": [round(w0), round(w1)]}
    if t["mirror_on"]:
        # The gateway doesn't report mirrored copies; the new pods' own sidecar does.
        sel = f'reporter="destination", namespace="{ns}", rollouts_pod_template_hash="{bad}"'
        out["mirrored"] = _requests(prom, sel, t["mirror_on"], (t["mirror_off"] or t["end"]) + SCRAPE_LAG_S)
    if tl["kind"] == "bluegreen":
        sel = (f'reporter="destination", namespace="{ns}", rollouts_pod_template_hash="{bad}", '
               'source_workload=~".*smoke-test.*"')
        out["smoke"] = _requests(prom, sel, t["start"], (t["exposed"] or t["end"]) + SCRAPE_LAG_S)
    return out


def measure(tl: dict, prom: Prometheus) -> dict:
    res = {"outcome": tl["outcome"], "bad_hash": tl.get("bad_hash"), "stable_hash": tl.get("stable_hash")}
    if "bad_hash" not in tl:
        return res
    t = _times(tl)
    caught_by, t_detect = _detection(tl, t)
    peak, exposed_s = _exposure_stats(tl)
    res |= {
        "caught_by": caught_by,
        # From the first request of any kind (or the start, if none ever got one).
        "ttd_s": _round(t_detect - (t["traffic"] or t["start"])) if t_detect else None,
        "detect_from_start_s": _round(t_detect - t["start"]) if t_detect else None,
        "ttr_s": _round(t["recover"] - t_detect) if t["recover"] and t_detect else None,
        "max_exposure_pct": peak,
        "user_exposed_s": _round(exposed_s),
        "release_duration_s": _round(t["end"] - t["start"]) if tl["outcome"] == "promoted" else None,
        "trial_pod_minutes": round(_trial_pod_minutes(tl), 2),
        "mirror_s": _round((t["mirror_off"] or t["end"]) - t["mirror_on"]) if t["mirror_on"] else None,
    }
    return res | _traffic(tl, t, prom)
