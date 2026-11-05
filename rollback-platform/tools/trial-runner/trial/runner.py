"""One trial, and the matrix of trials (docs/thesis-test-plan.md, section 8).

A trial: check the starting state, switch strategy if needed, trigger the
release through Git while the observer watches, measure, reset to the healthy
tag, save. Every step that changes something is a commit.
"""

import subprocess
import time
from datetime import datetime

from . import gitops, kube, measure, observe, results
from .config import App, Config
from .prom import Prometheus

SETTLE_S = 60  # between trials, so one run's 1m rate windows don't reach into the next


class TrialError(RuntimeError):
    pass


def _rollout(cfg: Config, app: App) -> dict:
    return observe.snapshot(kube.get_all(cfg.context, app.namespace), app)["rollout"]


def _image_tag(cfg: Config, app: App) -> str:
    out = subprocess.run(["kubectl", "--context", cfg.context, "-n", app.namespace, "get", "rollout",
                          app.rollout, "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
                         capture_output=True, text=True)
    return out.stdout.rsplit(":", 1)[-1]


def _settled(r: dict) -> bool:
    return r["phase"] in ("Healthy", "Degraded")


def _wait_healthy_on(cfg: Config, app: App, tag: str, timeout_s: float, log) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = _rollout(cfg, app)
        if r["phase"] == "Healthy" and r["current_hash"] == r["stable_hash"] and _image_tag(cfg, app) == tag:
            return
        time.sleep(cfg.poll_seconds * 2)
    raise TrialError(f"{app.name} not Healthy on {tag} after {timeout_s:.0f}s")


def reset(cfg: Config, app: App, log) -> None:
    """Back to the healthy reset tag.

    After an abort this is instant: Git points at the stable version again.
    After a promotion the bad image is stable, so the reset is a new release.
    It isn't measured, so on demo it runs under S0 (100% at once, seconds
    instead of up to 7.5 min); the next trial switches its strategy back.
    """
    r = _rollout(cfg, app)
    if r["phase"] == "Progressing":  # a release that never ended (e.g. F6): stop it first
        log(f"aborting the unfinished release of {app.name}")
        subprocess.run(["kubectl", "argo", "rollouts", "--context", cfg.context, "-n", app.namespace,
                        "abort", app.rollout], capture_output=True, text=True)
        r = _rollout(cfg, app)
    promoted_bad = r["current_hash"] == r["stable_hash"] and _image_tag(cfg, app) != app.reset_tag
    files = [app.kustomization]
    if promoted_bad and app.name == "demo" and gitops.set_strategy("s0"):
        files.append(gitops.DEV_OVERLAY)
    if gitops.set_tag(app, app.reset_tag) or len(files) > 1:
        how = "via s0" if len(files) > 1 else "back to stable"
        commit = gitops.commit_push(files, f"trial reset: {app.name} to {app.reset_tag} ({how})")
        gitops.refresh(cfg.context, app.name)
        log(f"reset commit {commit} ({how})")
    _wait_healthy_on(cfg, app, app.reset_tag, cfg.timeout_minutes * 60, log)


def _preconditions(cfg: Config, app: App, log) -> None:
    for other in cfg.apps.values():
        if other.name != app.name and not _settled(_rollout(cfg, other)):
            raise TrialError(f"{other.name} is still releasing")
    r = _rollout(cfg, app)
    if not (r["phase"] == "Healthy" and r["current_hash"] == r["stable_hash"]
            and _image_tag(cfg, app) == app.reset_tag):
        log(f"{app.name} isn't Healthy on the reset tag ({r['phase']}, {_image_tag(cfg, app)}): resetting")
        reset(cfg, app, log)


def run_id(strategy: str, fault: str, rep: int) -> str:
    return f"{strategy}-{fault}-r{rep}"


def run_trial(cfg: Config, prom: Prometheus, strategy: str, fault: str, rep: int = 1, log=print) -> dict:
    app = cfg.app_for(strategy)
    tag = cfg.faults[fault]
    rid = run_id(strategy, fault, rep)
    log(f"=== {rid}: {app.name} @ {tag}")
    _preconditions(cfg, app, log)

    if app.name == "demo" and gitops.set_strategy(strategy):
        commit = gitops.commit_push([gitops.DEV_OVERLAY], f"trial: strategy {gitops.component_dir(strategy)}")
        gitops.refresh(cfg.context, app.name)
        gitops.wait_synced(cfg.context, app.name, commit)
        log(f"strategy {strategy} active ({commit})")

    trigger = {}

    def push_release():
        gitops.set_tag(app, tag)
        trigger["commit"] = gitops.commit_push([app.kustomization], f"trial {rid}: {strategy}/{fault} ({tag})")
        gitops.refresh(cfg.context, app.name)
        log(f"trigger commit {trigger['commit']}")

    info = {"started_at": datetime.now().isoformat(timespec="seconds"), "app": app.name,
            "strategy": strategy, "fault": fault, "image_tag": tag, "rep": rep, "notes": ""}
    tl = observe.watch(cfg, app, prom, label=rid, start_timeout_s=300,
                       timeout_minutes=cfg.timeouts.get(fault), on_first_poll=push_release,
                       log=lambda s: log("   " + s))
    info["git_commit"] = trigger.get("commit")
    if tl["outcome"] == "not-started":
        raise TrialError(f"{rid}: the release didn't start within 5 min")
    if tl["outcome"] == "timeout":
        info["notes"] = "did not end by itself; aborted by the runner"
    m = measure.measure(tl, prom)
    results.save(rid, info, tl, m)
    log(f"--- {rid}: {m['outcome']}, caught by {m.get('caught_by')}, ttd {m.get('ttd_s')} s, "
        f"exposure {m.get('max_exposure_pct')}%, user 5xx {m.get('user', {}).get('5xx')}")

    reset(cfg, app, log)
    time.sleep(SETTLE_S)
    return m


def run_matrix(cfg: Config, prom: Prometheus, log=print) -> None:
    """All cells, grouped by strategy (fewer switches); runs already in the CSV are skipped."""
    done = results.done_run_ids()
    todo = [(c["strategy"], c["fault"], rep) for c in cfg.cells for rep in range(1, c.get("reps", 1) + 1)
            if run_id(c["strategy"], c["fault"], rep) not in done]
    log(f"matrix: {len(todo)} runs to do, {len(done)} already in results/trials.csv")
    for i, (s, f, rep) in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}]")
        run_trial(cfg, prom, s, f, rep, log)
    # Leave dev on the full setup, as it was before the matrix.
    if gitops.set_strategy("s7"):
        commit = gitops.commit_push([gitops.DEV_OVERLAY], "trial: matrix done, strategy s7-all")
        gitops.refresh(cfg.context, "demo")
        log(f"dev back on s7 ({commit})")
    log("matrix done")
