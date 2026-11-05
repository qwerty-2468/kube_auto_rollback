"""Watches one release and records a timeline of what changed and when.

A snapshot is a small summary of the app's namespace (rollout state, where
traffic goes, analysis runs, pods). Polling compares each snapshot with the
previous one and stores it only when something changed, so the timeline is a
list of state changes with local timestamps.
"""

import time
from datetime import datetime

from . import kube
from .config import App, Config
from .prom import Prometheus

HASH_LABEL = "rollouts-pod-template-hash"
CANARY_HOSTS = {"demo-canary", "demo-exp-canary"}  # destinations running the new version
RECOVERY_GRACE_S = 10  # keep watching this long after users are off the new version


def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None


def _named(objs, kind, name):
    return next(o for o in objs.get(kind, []) if o["metadata"]["name"] == name)


def _routes(vs: dict) -> dict:
    return {
        r.get("name", ""): {
            "weights": {d["destination"]["host"]: d.get("weight", 0) for d in r.get("route", [])},
            "mirror": (r.get("mirror") or {}).get("host"),
        }
        for r in vs["spec"].get("http", [])
    }


def _analysis(ar: dict) -> dict:
    s = ar.get("status", {})
    name = ar["metadata"]["name"]
    labels = ar["metadata"].get("labels") or {}
    metrics = {}
    for m in s.get("metricResults", []):
        ms = m.get("measurements", [])
        metrics[m["name"]] = {
            "phase": m.get("phase"),
            "count": m.get("count", len(ms)),
            "failed": m.get("failed", 0),
            "last_finished": ms[-1].get("finishedAt") if ms else None,
        }
    return {
        # The experiment's run carries no labels; name it by its role.
        "type": labels.get("rollout-type") or ("Experiment" if name.endswith("-compare") else "?"),
        "phase": s.get("phase"),
        "message": s.get("message"),
        "metrics": metrics,
    }


def _pod(p: dict) -> dict:
    m = p["metadata"]
    return {
        "hash": (m.get("labels") or {}).get(HASH_LABEL),
        "ready": any(c["type"] == "Ready" and c["status"] == "True"
                     for c in p.get("status", {}).get("conditions", [])),
        "created": m["creationTimestamp"],
        "deleting": bool(m.get("deletionTimestamp")),
    }


def snapshot(objs: dict[str, list[dict]], app: App) -> dict:
    st = _named(objs, "Rollout", app.rollout).get("status", {})
    current = st.get("currentPodHash")
    # Runs and experiments of the new version only; their names start with <rollout>-<hash>-.
    prefix = f"{app.rollout}-{current}-"
    snap = {
        "rollout": {
            "phase": st.get("phase"),
            "message": st.get("message"),
            "abort": bool(st.get("abort")),
            "step": st.get("currentStepIndex"),
            "current_hash": current,
            "stable_hash": st.get("stableRS"),
        },
        "analysis": {a["metadata"]["name"]: _analysis(a) for a in objs.get("AnalysisRun", [])
                     if a["metadata"]["name"].startswith(prefix)},
        "experiments": {e["metadata"]["name"]: e.get("status", {}).get("phase")
                        for e in objs.get("Experiment", []) if e["metadata"]["name"].startswith(prefix)},
        # The app's pods, including experiment pods (they copy the app label).
        "pods": {p["metadata"]["name"]: _pod(p) for p in objs.get("Pod", [])
                 if (p["metadata"].get("labels") or {}).get("app") == app.rollout},
    }
    if app.kind == "canary":
        snap["routes"] = _routes(_named(objs, "VirtualService", app.virtual_service))
    else:
        svc = _named(objs, "Service", app.active_service)
        snap["active_hash"] = svc["spec"].get("selector", {}).get(HASH_LABEL)
    return snap


def exposure(snap: dict, bad_hash: str | None) -> float:
    """Percent of user traffic on the new version."""
    if bad_hash and snap["rollout"]["stable_hash"] == bad_hash:
        return 100.0  # promoted: the stable Service (all users) now points at it
    if "routes" in snap:
        primary = snap["routes"].get("primary", {}).get("weights", {})
        return float(sum(w for h, w in primary.items() if h in CANARY_HOSTS))
    return 100.0 if bad_hash and snap.get("active_hash") == bad_hash else 0.0


def mirroring(snap: dict) -> bool:
    return any(r.get("mirror") for r in snap.get("routes", {}).values())


def started(snap: dict) -> bool:
    r = snap["rollout"]
    return r["current_hash"] is not None and r["current_hash"] != r["stable_hash"]


def aborted(snap: dict) -> bool:
    r = snap["rollout"]
    return r["abort"] or r["phase"] == "Degraded"


def _summary(snap: dict, bad_hash: str | None) -> str:
    r = snap["rollout"]
    runs = ",".join(f"{a['type']}:{a['phase']}" for a in snap["analysis"].values()) or "-"
    return (f"{r['phase']:<12} step={r['step']} exposure={exposure(snap, bad_hash):.0f}% "
            f"mirror={'on' if mirroring(snap) else 'off'} runs={runs}")


class _Release:
    """Decides, snapshot by snapshot, whether the release has started and ended."""

    def __init__(self, tl: dict):
        self.tl = tl
        self.recovered_at = None
        self.ignore_hash = None  # a release already under way (or aborted) when watching began

    def update(self, snap: dict) -> str | None:
        """Returns the outcome once the release is over, else None."""
        tl = self.tl
        if "bad_hash" not in tl:
            if not tl["events"] and started(snap):
                # After an aborted run the rollout still shows that release as
                # current; only a different hash is a new release.
                self.ignore_hash = snap["rollout"]["current_hash"]
            if started(snap) and snap["rollout"]["current_hash"] != self.ignore_hash:
                tl["bad_hash"] = snap["rollout"]["current_hash"]
                tl["stable_hash"] = snap["rollout"]["stable_hash"]
                tl["t_start"] = time.time()
            return None
        r = snap["rollout"]
        if r["phase"] == "Healthy" and r["stable_hash"] == tl["bad_hash"]:
            return "promoted"
        if aborted(snap):
            # Keep watching until users are off the new version, then a little longer.
            if self.recovered_at is None and exposure(snap, tl["bad_hash"]) == 0 and not mirroring(snap):
                self.recovered_at = time.time()
            if self.recovered_at and time.time() - self.recovered_at > RECOVERY_GRACE_S:
                return "aborted"
        return None


def watch(cfg: Config, app: App, prom: Prometheus, *, label: str = "",
          start_timeout_s: float = 600, timeout_minutes: float | None = None,
          on_first_poll=None, log=print) -> dict:
    """Waits for a release of `app` to start, follows it to the end, returns the timeline.

    on_first_poll is called once the starting state is recorded, so a trial can
    trigger its release there and the timeline still shows the state before it.
    """
    tl = {
        "app": app.name, "kind": app.kind, "namespace": app.namespace, "label": label,
        "clock_skew_s": round(prom.server_time() - time.time(), 3),  # cluster minus local
        "events": [],
    }
    release = _Release(tl)
    start_by = time.time() + start_timeout_s
    deadline = time.time() + (timeout_minutes or cfg.timeout_minutes) * 60
    last = None
    while True:
        snap = snapshot(kube.get_all(cfg.context, app.namespace), app)
        was_started = "bad_hash" in tl
        outcome = release.update(snap)
        if not was_started and "bad_hash" in tl:
            log(f"release started: {tl['stable_hash']} -> {tl['bad_hash']}")
        if snap != last:
            tl["events"].append({"t": time.time(), "snap": snap})
            log(f"{datetime.now():%H:%M:%S}  {_summary(snap, tl.get('bad_hash'))}")
            last = snap
        if on_first_poll:
            on_first_poll()
            on_first_poll = None
            start_by = time.time() + start_timeout_s  # count from the trigger, not from before it
        if outcome is None and "bad_hash" not in tl and time.time() > start_by:
            outcome = "not-started"
        if outcome is None and time.time() > deadline:
            outcome = "timeout"
        if outcome:
            break
        time.sleep(cfg.poll_seconds)
    tl["t_end"] = time.time()
    tl["outcome"] = outcome
    return tl
