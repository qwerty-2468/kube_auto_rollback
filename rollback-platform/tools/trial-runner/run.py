#!/usr/bin/env python3
"""Trial runner for the rollback strategy experiments. See README.md.

  run.py show                        print the parsed configuration and matrix
  run.py snapshot --app demo         print what the observer sees right now
  run.py observe --app demo [...]    wait for a release, follow it, measure, save
  run.py measure <timeline.json>     measure a saved timeline again (Prometheus keeps 24 h)
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from trial import config, gitops, kube, measure, observe, results, runner
from trial.prom import Prometheus

HERE = Path(__file__).resolve().parent


def _git_commit() -> str:
    out = subprocess.run(["git", "-C", str(config.REPO_ROOT), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()


def _image_tag(cfg, app) -> str:
    out = subprocess.run(["kubectl", "--context", cfg.context, "-n", app.namespace, "get", "rollout",
                          app.rollout, "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
                         capture_output=True, text=True)
    return out.stdout.rsplit(":", 1)[-1]


def cmd_show(cfg, _args):
    print(f"cluster: {cfg.context}, prometheus: {cfg.prometheus}")
    for a in cfg.apps.values():
        print(f"app {a.name}: {a.kind}, ns={a.namespace}, reset_tag={a.reset_tag}")
    print("faults:", json.dumps(cfg.faults, indent=1))
    for c in cfg.cells:
        tag = cfg.faults.get(c["fault"], "<no image>")
        print(f"cell {c['strategy']}/{c['fault']} x{c.get('reps', 1)}: {cfg.app_for(c['strategy']).name} @ {tag}")


def cmd_snapshot(cfg, args):
    app = cfg.apps[args.app]
    snap = observe.snapshot(kube.get_all(cfg.context, app.namespace), app)
    print(json.dumps(snap, indent=1))


def cmd_observe(cfg, args):
    app = cfg.apps[args.app]
    prom = Prometheus(cfg.prometheus, cfg.ca_file)
    run_id = args.run_id or f"{datetime.now():%Y%m%dT%H%M%S}-{args.strategy or 'x'}-{args.fault or 'x'}"
    info = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "app": app.name, "strategy": args.strategy, "fault": args.fault, "image_tag": args.tag,
        "rep": args.rep, "notes": args.notes,
    }
    print(f"run {run_id}: watching {app.namespace}/{app.rollout}, trigger the release now")
    tl = observe.watch(cfg, app, prom, label=run_id, start_timeout_s=args.start_timeout)
    if tl["outcome"] == "not-started":
        sys.exit(f"no release started within {args.start_timeout:.0f}s; nothing saved")
    info["git_commit"] = _git_commit()  # after the run, so it includes the trigger commit
    if not info["image_tag"]:  # e.g. a CI build, whose tag isn't known in advance
        info["image_tag"] = _image_tag(cfg, app)
    m = measure.measure(tl, prom)
    path = results.save(run_id, info, tl, m)
    print(json.dumps(m, indent=1))
    print(f"saved {path.relative_to(HERE)} and a row in results/trials.csv")


def _log(line: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {line}", flush=True)


def cmd_trial(cfg, args):
    runner.run_trial(cfg, Prometheus(cfg.prometheus, cfg.ca_file), args.strategy, args.fault, args.rep, _log)


def cmd_matrix(cfg, _args):
    runner.run_matrix(cfg, Prometheus(cfg.prometheus, cfg.ca_file), _log)


def cmd_measure(cfg, args):
    saved = json.loads(Path(args.timeline).read_text())
    m = measure.measure(saved["timeline"], Prometheus(cfg.prometheus, cfg.ca_file))
    print(json.dumps(m, indent=1))
    if args.save:
        info = saved["info"] | ({"git_commit": args.git_commit} if args.git_commit else {})
        results.save(saved["run_id"], info, saved["timeline"], m)
        print(f"replaced {saved['run_id']} in its JSON and results/trials.csv")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=HERE / "matrix.toml", type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("snapshot")
    s.add_argument("--app", required=True)
    o = sub.add_parser("observe", help="watch a release you trigger yourself")
    o.add_argument("--app", required=True)
    o.add_argument("--strategy", help="label for the CSV, e.g. s3")
    o.add_argument("--fault", help="label for the CSV, e.g. f1")
    o.add_argument("--tag", help="image tag being released")
    o.add_argument("--rep", type=int)
    o.add_argument("--run-id")
    o.add_argument("--notes", default="")
    o.add_argument("--start-timeout", type=float, default=600,
                   help="seconds to wait for the release to start (default 600)")
    t = sub.add_parser("trial", help="one cell: trigger through Git, watch, measure, reset")
    t.add_argument("--strategy", required=True, help="s0..s7 or bg")
    t.add_argument("--fault", required=True, help="f0..f8")
    t.add_argument("--rep", type=int, default=1)
    sub.add_parser("matrix", help="every cell in matrix.toml not yet in results/trials.csv")
    m = sub.add_parser("measure", help="measure a saved timeline again (Prometheus keeps 24 h)")
    m.add_argument("timeline")
    m.add_argument("--save", action="store_true", help="replace the run's JSON measurements and CSV row")
    m.add_argument("--git-commit", help="with --save: correct the recorded commit")
    args = p.parse_args()

    cfg = config.load(args.config)
    try:
        {"show": cmd_show, "snapshot": cmd_snapshot, "observe": cmd_observe, "trial": cmd_trial,
         "matrix": cmd_matrix, "measure": cmd_measure}[args.cmd](cfg, args)
    except kube.KubeError as e:
        sys.exit(f"kubectl failed (NetBird up?): {e}")
    except (gitops.GitError, runner.TrialError) as e:
        sys.exit(f"stopped: {e}")


if __name__ == "__main__":
    main()
