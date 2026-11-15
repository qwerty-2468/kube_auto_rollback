"""One trial, and the matrix of trials (docs/thesis-test-plan.md, section 8).

A trial: check the starting state, switch strategy if needed, trigger the
release through Git while the observer watches, measure, reset to the healthy
release, save. Every step that changes something is a commit.
"""

import subprocess
import time
from datetime import datetime

from . import gitops, kube, measure, observe, results
from .config import App, Config
from .prom import Prometheus

SETTLE_S = 60  # between trials, so one run's 1m rate windows don't reach into the next
FAST = {"demo": "s0", "demo-bg": "b0"}  # no analysis: a reset that is a new release takes seconds
DEFAULT = {"demo": "s4", "demo-bg": "b3"}  # the base configuration, left on after the matrix


class TrialError(RuntimeError):
    pass


def _rollout(cfg: Config, app: App) -> dict:
    return observe.snapshot(kube.get_all(cfg.context, app.namespace), app)["rollout"]


def _live(cfg: Config, app: App, jsonpath: str) -> str:
    out = subprocess.run(["kubectl", "--context", cfg.context, "-n", app.namespace, "get", "rollout",
                          app.rollout, "-o", f"jsonpath={{.spec.template.spec.containers[0].{jsonpath}}}"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def _image_tag(cfg: Config, app: App) -> str:
    return _live(cfg, app, "image").rsplit(":", 1)[-1]


def _release(cfg: Config, app: App) -> str:
    """The APP_VERSION of the release the cluster runs."""
    return _live(cfg, app, 'env[?(@.name=="APP_VERSION")].value')


def _settled(r: dict) -> bool:
    return r["phase"] in ("Healthy", "Degraded")


def _wait_healthy_on(cfg: Config, app: App, release: str, timeout_s: float, log) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = _rollout(cfg, app)
        if r["phase"] == "Healthy" and r["current_hash"] == r["stable_hash"] and _release(cfg, app) == release:
            return
        time.sleep(cfg.poll_seconds * 2)
    raise TrialError(f"{app.name} not Healthy on {release} after {timeout_s:.0f}s")


def reset(cfg: Config, app: App, log) -> None:
    """Back to the healthy reset release.

    After an abort this is instant: Git points at the stable version again.
    After a promotion the bad release is stable, so the reset is a new release.
    It isn't measured, so it runs without any analysis (S0 or B0: seconds
    instead of minutes); the next trial switches its strategy back.
    """
    r = _rollout(cfg, app)
    if r["phase"] == "Progressing":  # a release that never ended (e.g. F6): stop it first
        log(f"aborting the unfinished release of {app.name}")
        subprocess.run(["kubectl", "argo", "rollouts", "--context", cfg.context, "-n", app.namespace,
                        "abort", app.rollout], capture_output=True, text=True)
        r = _rollout(cfg, app)
    name = cfg.reset["APP_VERSION"]
    promoted_bad = r["current_hash"] == r["stable_hash"] and _release(cfg, app) != name
    files = [app.release]
    if promoted_bad and gitops.set_strategy(app, FAST[app.name]):
        files.append(gitops.dev_overlay(app))
    if gitops.set_release(app, cfg.reset) or len(files) > 1:
        how = f"via {FAST[app.name]}" if len(files) > 1 else "back to stable"
        commit = gitops.commit_push(files, f"trial reset: {app.name} to {name} ({how})")
        gitops.refresh(cfg.context, app.name)
        log(f"reset commit {commit} ({how})")
    _wait_healthy_on(cfg, app, name, cfg.timeout_minutes * 60, log)


def _preconditions(cfg: Config, app: App, log) -> None:
    for other in cfg.apps.values():
        if other.name != app.name and not _settled(_rollout(cfg, other)):
            raise TrialError(f"{other.name} is still releasing")
    r = _rollout(cfg, app)
    if not (r["phase"] == "Healthy" and r["current_hash"] == r["stable_hash"]
            and _release(cfg, app) == cfg.reset["APP_VERSION"]):
        log(f"{app.name} isn't Healthy on the reset release ({r['phase']}, {_release(cfg, app)}): resetting")
        reset(cfg, app, log)


def run_id(strategy: str, fault: str, rep: int) -> str:
    return f"{strategy}-{fault}-r{rep}"


def run_trial(cfg: Config, prom: Prometheus, strategy: str, fault: str, rep: int = 1, log=print) -> dict:
    app = cfg.app_for(strategy)
    env = cfg.release_env(fault)
    name = env["APP_VERSION"]
    rid = run_id(strategy, fault, rep)
    log(f"=== {rid}: {app.name} @ {name}")
    _preconditions(cfg, app, log)

    if gitops.set_strategy(app, strategy):
        commit = gitops.commit_push([gitops.dev_overlay(app)],
                                    f"trial: {app.name} strategy {gitops.component_dir(app, strategy)}")
        gitops.refresh(cfg.context, app.name)
        gitops.wait_synced(cfg.context, app.name, commit)
        log(f"strategy {strategy} active ({commit})")

    trigger = {}

    def push_release():
        gitops.set_release(app, env)
        trigger["commit"] = gitops.commit_push([app.release], f"trial {rid}: {strategy}/{fault} ({name})")
        gitops.refresh(cfg.context, app.name)
        log(f"trigger commit {trigger['commit']}")

    info = {"started_at": datetime.now().isoformat(timespec="seconds"), "app": app.name,
            "strategy": strategy, "fault": fault, "image_tag": _image_tag(cfg, app), "rep": rep, "notes": ""}
    tl = observe.watch(cfg, app, prom, label=rid, start_timeout_s=300,
                       timeout_minutes=cfg.timeouts.get(fault), on_first_poll=push_release,
                       log=lambda s: log("   " + s))
    info["git_commit"] = trigger.get("commit")
    if tl["outcome"] == "not-started":
        raise TrialError(f"{rid}: the release didn't start within 5 min")
    if tl["outcome"] == "timeout":
        info["notes"] = "did not end by itself; aborted by the runner"
    # The user window reaches SCRAPE_LAG_S past the end (a promoted release
    # ends right after the switch), so wait until Prometheus has those samples.
    time.sleep(max(0.0, tl["t_end"] + measure.SCRAPE_LAG_S + 5 - time.time()))
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
    # Leave dev on the base configuration.
    for app in cfg.apps.values():
        if gitops.set_strategy(app, DEFAULT[app.name]):
            commit = gitops.commit_push([gitops.dev_overlay(app)],
                                        f"trial: matrix done, {app.name} back on {DEFAULT[app.name]}")
            gitops.refresh(cfg.context, app.name)
            log(f"{app.name} back on {DEFAULT[app.name]} ({commit})")
    log("matrix done")
