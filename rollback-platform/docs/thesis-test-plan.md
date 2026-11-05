# Test plan: comparing automated rollback strategies

Status: draft, 2026-11-01. The hypotheses (section 4) are written **before** any matrix run
and should stay as they are; results are compared against them afterwards.
Related: [progressive-delivery-tests.md](progressive-delivery-tests.md) covers the experiment
step, `demo-bg` and the Job provider as built.

## 1. Research questions

| # | Question | Answered by |
|---|---|---|
| RQ1 | Which detection mechanism catches which kind of fault, and how fast? | the single-mechanism strategies S1–S3: detected?, by which check, time to detect |
| RQ2 | What does each strategy cost in user exposure and in release time and resources? | blast radius, exposure, healthy-release duration, pod-minutes |
| RQ3 | Absolute vs relative analysis: which gives fewer false alarms and fewer misses? | F0 (false alarms), F2 and F5 (misses), S1 (absolute) compared with S3 (relative) |
| RQ4 | Do combined mechanisms protect better than single ones, and which combinations add nothing? | S4–S7 compared with S1–S3 |
| RQ5 | Gradual vs all-at-once: what does blue-green change? | BG compared with the canary family |

A parameter sub-study (first canary weight, analysis interval) can be added later as configs
A–D in the Blast Radius Notebook.

## 2. Strategies

Three detection mechanisms can each be on or off:

| Mechanism | Steps | Check | Users exposed while it runs |
|---|---|---|---|
| **M** mirroring | setCanaryScale 1 → mirror 100% of `GET /api/work` → `mirror-health` (3 × 20 s) → mirror off | absolute: success ≥ 95%, p95 ≤ 300 ms | 0% (copies only) |
| **E** experiment | experiment 3 min: baseline 20% + canary 20% | relative `canary-vs-baseline`: deltas for errors, latency, memory | 20% on the new version |
| **C** canary | setWeight 10 → pause 1 min → setWeight 50 → pause 1 min | absolute background `canary-health`: success, p95, memory growth | 10%, then 50% |

They always run in the order **M → E → C**. A release has to end at 100%: when the last
step finishes, the Rollout promotes. So a strategy without C goes **straight to 100%** after its
last check, and a fault that check misses reaches every user. That's the honest standalone
behaviour of the mechanism.

All 8 combinations are tested (a full factorial design), plus blue-green:

| ID | M | E | C | Shows | Component |
|---|---|---|---|---|---|
| S0 | | | | control: no progressive delivery | `s0-none` |
| S1 | | | ✓ | canary alone | `s1-canary` |
| S2 | ✓ | | | mirroring alone | `s2-mirror` |
| S3 | | ✓ | | experiment alone | `s3-experiment` |
| S4 | ✓ | | ✓ | mirror + canary | `s4-mirror-canary` |
| S5 | | ✓ | ✓ | experiment + canary | `s5-experiment-canary` |
| S6 | ✓ | ✓ | | mirror + experiment, no canary | `s6-mirror-experiment` |
| S7 | ✓ | ✓ | ✓ | all three (`base/rollout.yaml` as it is) | `s7-all` |
| BG | | | | blue-green: preview → smoke Job → switch → `bg-post-promotion` | app `demo-bg` |

**Kept fixed in every strategy:**
- the parameters of each mechanism (mirror count and interval, experiment duration and
  weights, canary steps and pauses), so a difference between two strategies comes only from
  the mechanisms that differ;
- `progressDeadlineSeconds: 120` with `progressDeadlineAbort`. It's a property of the
  Rollout, not a step, so even S0 has it. S0 therefore means "platform defaults, no
  progressive delivery", and F6 shows the deadline working independently of the strategy.

## 3. Faults

Scope: faults in the **release** only. The environment fault F7 (healthy release
on a CPU-stressed node) was dropped on 2026-10-31; its ID is kept free so the others
don't change.

| ID | Fault | Build setting (demo `release.env`) | Category | Why it's included |
|---|---|---|---|---|
| F0 | healthy | none | control | false positives, cost of a good release |
| F1 | hard errors | `ERROR_RATE=30` | fast, loud | the basic case |
| F2 | subtle errors | `ERROR_RATE=2` | below threshold | a deliberate miss by the threshold |
| F3 | constant latency | `LATENCY_MS=800` | fast, loud | |
| F4 | tail latency | `LATENCY_JITTER_MS=1500` | noisy | p95 behaviour, detection variance |
| F5 | memory leak | `LEAK_MB_PER_MIN=20` | slow-burn | short checks miss it |
| F6 | never Ready | `FAIL_READINESS=true` | startup | progress deadline, not metrics |
| F8 | wrong answer with HTTP 200 | `WRONG_ANSWER=true` (**to be added to the app**) | semantic | blind spot of metric analysis; only a functional check sees it |

## 4. Hypotheses

Each cell: **the check that stops the release · the share of users exposed when it does.**
✗ means not caught, so the release reaches 100% of users. With combined mechanisms, the first
one in the order M → E → C that catches the fault decides; the later ones never run.

| | F0 healthy | F1 errors 30% | F2 errors 2% | F3 latency 800 ms | F4 jitter 1500 ms | F5 leak | F6 never Ready | F8 wrong answer |
|---|---|---|---|---|---|---|---|---|
| **S0** none | promoted | ✗ 100% | ✗ 100% | ✗ 100% | ✗ 100% | ✗ 100% | deadline · ?%¹ | ✗ 100% |
| **S1** C | promoted | C · 10% | ✗ 100% | C · 10% | C · 10%² | C · 50%³ | deadline · 0% | ✗ 100% |
| **S2** M | promoted | M · 0% | ✗ 100% | M · 0% | M · 0%² | ✗ 100%⁴ | deadline · 0%⁵ | ✗ 100% |
| **S3** E | promoted | E · 20% | ✗ 100% | E · 20% | E · 20% | E · 20% | deadline · 0% | ✗ 100% |
| **S4** M+C | promoted | M · 0% | ✗ 100% | M · 0% | M · 0% | C · 50% | deadline · 0% | ✗ 100% |
| **S5** E+C | promoted | E · 20% | ✗ 100% | E · 20% | E · 20% | E · 20% | deadline · 0% | ✗ 100% |
| **S6** M+E | promoted | M · 0% | ✗ 100% | M · 0% | M · 0% | E · 20% | deadline · 0% | ✗ 100% |
| **S7** M+E+C | promoted | M · 0% | ✗ 100% | M · 0% | M · 0% | E · 20% | deadline · 0% | ✗ 100% |
| **BG** | promoted | Job · 0% | Job · 0% (p ≈ 64%)⁶ | post · 100% | post · 100% | post · 100% | deadline · 0% | Job · 0%⁷ |

Every F2 ✗ is **by design**: 2% is below every threshold (success ≥ 95%, or a delta ≤ 5%).

Expected duration of a healthy release (F0), the time cost of each strategy:

| | S0 | S1 | S2 | S3 | S4 | S5 | S6 | S7 | BG |
|---|---|---|---|---|---|---|---|---|---|
| time | ~10 s | ~2.5 min | ~1.5 min | ~3.5 min | ~4 min | ~6 min | ~5 min | ~7.5 min | ~3 min |

Cells the runs have to settle:
1. **S0 + F6:** with no steps the canary may get weight 100 before its pods are Ready, so
   Envoy has no healthy endpoints and users get 503s until the deadline abort; or Rollouts
   waits for Ready first (0%). The most interesting S0 cell.
2. **F4:** a random 0–1500 ms delay gives a p95 of about 1400 ms, so it should be caught, but
   with few samples per window, detection is slower and varies more than for F3.
3. **S1 + F5:** `memory-growth` starts at 90 s, by which time the canary is at 50%.
4. **S2 + F5:** `mirror-health` has no memory check and lasts about 70 s, so mirroring alone
   lets a leak through to everyone.
5. **M + F6:** the canary pod never receives mirrored traffic. Either `mirror-health` fails on
   "no data" after about 30 s, or the deadline aborts at 120 s, depending on whether Rollouts
   waits for Ready. Either way 0%.
6. **BG + F2:** 0 failures out of 50 passes with probability 0.98⁵⁰ ≈ 36%, so it's caught in
   about 64% of runs; one run shows one of the two outcomes.
7. **BG + F8:** only with the body check in the smoke Job.

What the comparisons answer:

| Compare | Question | Expected answer |
|---|---|---|
| S1 / S2 / S3 | which fault classes each mechanism catches alone (RQ1) | M: errors and latency at 0%, blind to leaks. E: all metric faults at 20%. C: all metric faults at 10–50% |
| S1 → S4 | what mirroring adds in front of the canary | F1, F3, F4 move from 10% to 0% |
| S1 → S5 | what the experiment adds in front of the canary | F5 50% → 20%, but F1, F3, F4 get **worse** (10% → 20%) |
| S4 → S7 | what the experiment adds to mirror + canary | F5 50% → 20%, for +3.5 min per release |
| S6 vs S7 | does the canary still matter after M + E? | no cell changes: pure time cost |
| S1 vs S3 | absolute vs relative (RQ3) | same faults caught; E exposes more users, C catches F5 later |
| canary family vs BG | gradual vs all-at-once (RQ5) | BG 0% before the switch but 100% after it (F3–F5) |
| columns with only ✗ | the limits of metric analysis | F2 (below threshold), F8 (only the BG Job's body check) |

The S1 → S5 prediction is a design finding in itself: an experiment placed before the first
small canary step, at a higher weight, exposes more users than the canary would. Put it after
`setWeight: 10`, or give it a lower weight. Only the single-mechanism strategies make this
visible.

## 5. Measurements

| Measurement | Definition | Source |
|---|---|---|
| detected | the rollout ended Aborted | Rollout `status` |
| caught_by | the first failing metric, or `progress-deadline` | AnalysisRun `metricResults` |
| TTD (time to detect) | first request of any kind to the bad version → the failing measurement | runner timeline |
| TTR (time to recover) | detection → 0% user traffic on the bad version | runner timeline (VS weights or active selector) |
| max_exposure | highest share of user traffic on the bad version | step weights in the timeline |
| blast_radius | user requests that were 5xx or slower than 500 ms (the nearest Istio histogram bucket; there is none at 300), counted exactly from counter differences | Prometheus |
| release_duration | healthy release, from start until 100% | runner timeline |
| trial_pod_minutes | lifetime of the pods created during the trial, until it ended | runner timeline |
| false-positive rate | aborted F0 runs ÷ all F0 runs | derived |

Exact definitions: [tools/trial-runner/README.md](../tools/trial-runner/README.md).

**Resolution:** scrape 10s, analysis interval 20–30s, `rate` windows 1m. A TTD below about
30s can't be measured reliably; report this with the results.

## 6. Procedure

- Only one app is releasing at a time; the other sits idle on a healthy tag.
- loadgen runs at a fixed rate (about 5 requests per second, unchanged between runs).
- Every run starts from Healthy on the app's reset tag and ends by resetting to it.
- Each run records the Git commit, so the strategy and image can be traced.
- **One run per cell.** Most outcomes follow from how the checks are built (thresholds,
  windows, step order), so they are expected to repeat. Where an outcome is random, the
  result is reported with its calculated probability instead of a measured spread: for
  example F2 under BG is caught with probability 1 − 0.98⁵⁰ ≈ 64%. A cell can be repeated
  on its own if its result contradicts the hypothesis.
- **Matrix:** 9 strategies (S0–S7, BG) × 8 faults (F0–F6, F8) = **72 runs**. At about
  8 minutes each, including the reset, that's 9–10 h of rollouts, run by the trial runner
  (section 8).

## 7. Switching strategies

```
gitops/apps/demo/components/
  s0-none/                 steps: [], no background analysis
  s1-canary/               C
  s2-mirror/               M, then 100%
  s3-experiment/           E, then 100%
  s4-mirror-canary/        M + C
  s5-experiment-canary/    E + C
  s6-mirror-experiment/    M + E, then 100%
  s7-all/                  M + E + C   (no-op: base/rollout.yaml is S7)
gitops/apps/demo/overlays/dev/kustomization.yaml
  components: [../../components/s7-all]     <- exactly one, one-line change per switch
```

How it's built:
- `base/rollout.yaml` **is** S7 and stays unchanged; prod keeps its own step list.
- Every other component replaces the whole step list, so each file is the complete definition
  of its strategy and can be quoted as it is. The M, E and C blocks are identical in every file.
- **With C**, `canary-health` runs in the background from the pause after `setWeight: 10`:
  `startingStep` 1 (S1), 2 (S5), 6 (S4), 7 (S7). **Without C** (S0, S2, S3, S6), the component
  removes the background analysis, so only the strategy's own mechanisms can catch anything.
- `s7-all` is a no-op component (`patches: []`; Kustomize rejects one with no fields as "empty").
  It exists so the dev overlay always names the strategy under test.
- Switching strategy changes only the Rollout's strategy, not its pod template, so it
  doesn't start a rollout.

BG is the app `demo-bg`, which already exists.

## 8. Automated trial runner

Stages R1–R3 (observe and measure a release triggered by hand) are built and validated; see
[tools/trial-runner/README.md](../tools/trial-runner/README.md). R4–R5 below are still to do.

### Interface (R4–R5)
```sh
tools/trial-runner/run.py trial --strategy s4 --fault f1      # one cell
tools/trial-runner/run.py matrix                              # all cells in matrix.toml
```
- `matrix.toml` (TOML because `tomllib` is in the standard library; YAML would need PyYAML):
  the cells, the fault → image tag map, and each app's reset tag.
- Output: one CSV row per run in `results/trials.csv`, plus a JSON timeline per run in
  `results/timelines/<run-id>.json`.

### Why it changes Git instead of the cluster
Argo CD self-heals, so `kubectl argo rollouts set image` would be reverted. Every trial
therefore works through Git: set the strategy component or `newTag`, commit, push, then
refresh Argo CD. This keeps the setup GitOps-correct, and every run is a commit that can
be cited in the thesis. The runner rebases before each push (`pull.rebase true`), because
CI also commits to `main`.

### Flow for one run
1. **Preconditions:** the app is Healthy on its reset tag and the other app is idle; abort
   if not.
2. **Strategy:** if the active component differs, change it, commit, push, refresh, and wait
   until Argo CD reports Synced. A strategy-only change doesn't start a rollout.
3. **Trigger:** set `newTag` to the fault image, commit (`trial <run-id>: <strategy>/<fault>`),
   push, refresh.
4. **Observe:** poll every 2s until promoted or aborted (or a 20-minute timeout), recording
   each state change: Rollout phase, step and abort flag; VirtualService weights (canary) or
   the active Service's selector (BG); AnalysisRun and Experiment phases; pods.
5. **Measure:** section 5, from the timeline and Prometheus, immediately (Prometheus keeps
   24 h).
6. **Reset:** set `newTag` back to the reset tag, push, refresh, and wait for Healthy. Wait
   another 60s, so the next run's 1m windows don't overlap this one.
7. **Save:** the JSON timeline and the CSV row.

### CSV columns
```
run_id, started_at, git_commit, app, strategy, fault, image_tag, rep,
outcome, caught_by, ttd_s, detect_from_start_s, ttr_s, max_exposure_pct, user_exposed_s,
user_requests, user_5xx, user_slow, mirrored_requests, mirrored_5xx, smoke_requests, smoke_5xx,
release_duration_s, trial_pod_minutes, notes
```

### Choices
- **Python 3, standard library only:** `subprocess` for `kubectl … -o json` and `git`, and
  `urllib` for the Prometheus HTTP API.
- **Runs from the Mac** over NetBird. If NetBird drops, the run is marked `timeout` and the
  matrix stops, rather than recording bad data.
- **Resumable:** a run already in the CSV is skipped, so a long matrix can be restarted.

## 9. Advantages and drawbacks (for the discussion chapter)

| | Advantages | Drawbacks |
|---|---|---|
| C canary | real traffic, gradual, cheap | some users always reach the bug; thresholds need tuning per service; slow faults are caught late (F5 at 50%) |
| M mirroring | 0% user exposure for errors and latency | idempotent requests only (GET); responses are thrown away; double downstream load; too short to catch leaks |
| E experiment | the relative check cancels startup effects; catches leaks early | +2 pods and +3 min per release; exposes 20% before any small step; noisy at low traffic; misses "both are bad", so an absolute floor stays necessary |
| BG blue-green | simple model; instant, complete rollback; the preview can be tested by people or scripts | 2× resources while switching; post-promotion faults reach 100% of users; the preview gets no real traffic |
| Job provider | can check behaviour, not only metrics (F8) | synthetic traffic only; small sample (F2 sometimes passes); needs the native sidecar in a STRICT mesh |

**Argument: defence in depth, with evidence.** S1–S3 show what each mechanism catches on its
own; S4–S7 show which combinations add protection and which only add time. Planned figure:
the mechanisms drawn as stacked filters, with each fault class marked at the layer that stops it.

**Practical result: a decision table.** For example: read-heavy service, user errors
unacceptable → M first (S4); slow-burn faults expected → E, placed after a small canary step;
resource-constrained → S1.

**Planned charts:** an exposure vs TTD scatter plot (one point per run, coloured by strategy)
and a detection-matrix heatmap (strategy × fault: caught or not, and by what).

## 10. Threats to validity and future work

**Threats to validity:**
- one run per cell: no measured spread for TTD or blast radius, and random outcomes
  (F2, F4) rest on a single sample
- release faults only: environment faults (for example a CPU-stressed node, where relative
  analysis should avoid false alarms) are out of scope
- a single node, with 1 replica per version
- synthetic traffic at about 5 requests per second, so statistical power is low
- faults injected at build time, so they're deterministic
- 10s measurement resolution
- the mechanism order M → E → C is fixed; other orders (for example E after the first canary
  step) are not tested
- prod not built

**Future work:**
- statistical canary analysis (Kayenta / Mann-Whitney) instead of fixed thresholds
- dark launch with a baseline (Diffy-style mirroring to baseline and canary)
- header-based routing for internal testers
- Rollouts notifications
- multi-replica and multi-node setups
- real traffic patterns

## 11. Work order

| Phase | Work | Rough time |
|---|---|---|
| 1 | Freeze the matrix and hypotheses (this document) | done |
| 2 | Components S0–S7 | done |
| 3 | `WRONG_ANSWER` fault in the app + body check in the smoke Job | 1–2 h |
| 4 | Build fault images F2–F6, F8 (each CI build doubles as a pilot run) | 2 h |
| 5 | Trial runner R4–R5 (section 8), validated on 3–4 cells | 0.5–1 day |
| 6 | Run the matrix (mostly unattended) | 1–2 days elapsed |
| 7 | Tables, charts, discussion | 1–2 days |

## Decisions

1. S0 (control) is included (2026-10-31).
2. F8 (wrong answer with HTTP 200) is included; it needs the app change and a body check in
   the smoke Job (2026-10-31).
3. One run per cell (2026-10-31).
4. Strategies are switched with Kustomize components (2026-10-31).
5. Release faults only; F7 dropped (2026-10-31).
6. Full factorial design over M, E and C, S6 included; blue-green renamed BG (2026-11-01).
7. The progress deadline stays on in every strategy, including S0 (2026-11-01).
