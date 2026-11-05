# Trial runner

Measures rollouts for the strategy × fault matrix in
[docs/thesis-test-plan.md](../../docs/thesis-test-plan.md). Python 3.11+, standard
library only. It runs on the Mac and needs NetBird up, the `k3s-dev` kube context and
`https://prometheus.dev.internal`.

**Status:** done. The full matrix (72 runs) ran on 2026-11-01, 13:15–17:35, and every
row was re-measured afterwards with the measurement fixes listed under Validation.

## Usage

```sh
cd tools/trial-runner
./run.py show                          # parsed matrix.toml
./run.py snapshot --app demo           # what the observer sees right now
./run.py observe --app demo-bg --strategy bg --fault f1 --tag v5-3e32cfe-6 --rep 1
#   ... then trigger the release (newTag commit + push + refresh) in another terminal
./run.py measure results/timelines/<run-id>.json   # measure again (within 24 h)
```

`observe` waits up to 10 minutes for the release to start, follows it every 2 seconds
until it is promoted or aborted (plus 10 seconds after users are off it), measures it,
and saves:
- `results/timelines/<run-id>.json`: every state change, plus the measurements and the
  raw Prometheus counts. This file is the record of the run, because Prometheus forgets
  after 24 hours.
- one row in `results/trials.csv`.

`results/` is in `.gitignore`, so the thesis data lives only on this machine. Back it up:
Prometheus keeps 24 hours, so a lost run can't be measured again.

## How it works

| Module | Job |
|---|---|
| `trial/config.py` | reads `matrix.toml`: cluster, apps, fault → image tag, cells |
| `trial/kube.py` | one `kubectl get rollout,analysisrun,experiment,virtualservice,service,pod -o json` per poll (~0.3 s) |
| `trial/prom.py` | Prometheus instant queries; trusts `docs/ca/dev-root-ca.crt` explicitly, because Python doesn't use the macOS keychain |
| `trial/observe.py` | turns each poll into a small **snapshot** and keeps it only when it changed, giving a **timeline** |
| `trial/measure.py` | timeline + Prometheus → the measurements |
| `trial/results.py` | JSON per run, CSV row, list of runs already done |

### Snapshot
- **Rollout:** phase, abort flag, step, current and stable hash.
- **Where users go:** canary → weights of the VirtualService's `primary` route, plus any
  mirror. Blue-green → the hash the **active Service** selects (the Service itself, not the
  Rollout's status, because the Service is what actually routes users).
- **The new version's AnalysisRuns and Experiments:** matched by name prefix
  `<rollout>-<hash>-`, because the experiment's AnalysisRun has no labels.
- **The app's pods,** including experiment pods.

### Measurements

| Field | How |
|---|---|
| `outcome` | `promoted` when stable = the new hash and Healthy; `aborted` when the abort flag is set or Degraded; `timeout` after 20 min |
| `caught_by` | `<run type>/<metric>` of the failing metric that finished first; `progress-deadline` if the rollout aborted because its ReplicaSet timed out, `Experiment/progress-deadline` if an experiment template's pods did |
| `ttd_s` | detection time minus the **first request of any kind** to the new version: a mirrored copy, a user request, or the smoke Job. Detection time is the failing measurement's `finishedAt`, which is cluster time converted with the measured clock skew |
| `detect_from_start_s` | the same from the release start (the only option for F6, which never gets traffic) |
| `ttr_s` | from detection until no users and no mirror reach the new version |
| `max_exposure_pct`, `user_exposed_s` | peak share of users on the new version (100% once it is promoted, since the stable Service then points at it), and for how long any were before the release ended |
| `user_*` | requests the gateway sent to the new version, read from the new pods' sidecar by their hash (so canary weight and the stable Service after promotion both count), plus the experiment's canary pods through `demo-exp-canary`. Window: from first exposure (after mirroring ended) until recovery, or for promoted runs until 30 s after promotion; a promoted fault then stays with every user until someone acts |
| `mirrored_*`, `smoke_*` | copies received by the new pods during mirroring; the smoke Job's requests |
| `*_slow` | over **500 ms**: Istio's buckets jump from 250 to 500, so there is no exact 300 ms boundary |
| `release_duration_s` | promoted runs: start → promoted |
| `trial_pod_minutes` | lifetime of pods created during the trial, until the trial ended (a snapshot holds until the next one, so a pod alive at the end counts until `t_end`) |

**Request counts are exact.** Each count is a counter's last value up to the end of the
window minus its last value before the start. `increase()` would extrapolate to the window
edges. Checked against run B1: `increase()` gave 63 smoke requests and 664 user requests;
the exact method gives 50 (matching the Job's log) and 629. Windows end 30 s after the event,
to cover the 10 s scrape.

### Validation done (2026-10-31)
- `show` and `snapshot` against the live `demo` and `demo-bg`.
- B1 replayed from its real timestamps against live Prometheus: promoted, 100% exposure,
  161 s, 50 smoke requests, 629 user requests with 0 errors and 0 slow.
- A synthetic aborted canary with mirroring: `caught_by` Step/mirror-success-rate, TTD 40 s,
  TTR 4 s, 0% exposure. This is the same shape as run A1.
- Live run B2 (blue-green, v5): caught by PrePromotion/smoke-test, TTD 14.7 s, TTR 1.2 s,
  0% exposure, 50 smoke requests with 18 of them 5xx. It found two bugs, both fixed and the
  row re-measured with `measure --save`:
  - snapshots taken before the start showed the previous release's runs, so measuring now
    starts at `t_start`;
  - the Git commit is now recorded after the run, so it includes the trigger commit.

### Fixes found by the matrix (2026-11-01), all rows re-measured
- **Promoted releases showed 0% exposure.** After promotion the canary weight goes back to 0
  but the stable Service points at the new version. Exposure is now 100% once
  stable = the new hash.
- **User requests were undercounted for promoted runs.** They were counted on the canary
  Service only. Now: the new pods' sidecar (destination reporter) by hash, from the
  gateway, plus the experiment's canary Service.
- **F6 aborts were labelled with the raw message**; now `progress-deadline` or
  `Experiment/progress-deadline`.
- **`trial_pod_minutes` was 0 for a pod seen in one snapshot only** (the F6 timeouts);
  each snapshot now lasts until the next.
