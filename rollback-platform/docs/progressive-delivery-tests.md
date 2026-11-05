# Progressive delivery tests: experiment step, blue-green, Job provider

What was added on 2026-10-30 to test three more Argo Rollouts features next to the
existing canary (mirror phase + `canary-health`), why each piece looks the way it does,
and how to test it on dev.

| Feature | Where | Question it answers |
|---|---|---|
| Experiment step | `demo` canary, between the mirror phase and `setWeight: 10` | Is the new version *worse than the old one* under the same conditions? (relative analysis) |
| Blue-green | new app `demo-bg` (namespace `demo-bg`) | What changes when all users switch at once instead of gradually? |
| Job provider | `demo-bg` pre-promotion check | Can a scripted smoke test gate a release before any user sees it? |

Nothing here has been committed or pushed yet. See [Before testing](#before-testing).

## 1. Changes to `demo` (the canary)

### New file `gitops/apps/demo/base/experiment-analysis-template.yaml`
AnalysisTemplate `canary-vs-baseline`, three relative metrics:

| Metric | Query (simplified) | Passes when |
|---|---|---|
| `error-delta` | 5xx share of `demo-exp-canary` minus 5xx share of `demo-exp-baseline` | ≤ 0.05 |
| `latency-delta` | p95 of canary minus p95 of baseline, ms | ≤ 100 |
| `memory-delta` | memory growth of canary pods minus baseline pods, MiB/s | ≤ 0.05 |

- **Why a difference and not a ratio for latency:** the app answers in a few ms. A ratio
  would call 4 ms vs 7 ms a 1.75× regression.
- **Traffic metrics** use `reporter="source"`, meaning the gateway's view, filtered by
  `destination_service_name`. The experiment Services have fixed names, so the queries are exact.
- **Memory** selects pods by name (`pod=~"demo-.+-canary-.*"` and `…-baseline-.*`).
  cAdvisor series carry no pod labels, and there is no kube-state-metrics for a join on the hash
  label. Normal canary pods (`demo-<hash>-<random>`) don't match these patterns.
- **`dryRun: [{metricName: ".*"}]`** (first runs only): every metric was recorded and none could
  fail the run. It was removed after A0 showed sensible values (error-delta 0, latency-delta
  −1 to +6 ms, memory-delta ≈ 0).
- **`len(result) > 0 &&`**: an empty query result fails the check cleanly instead of
  causing an evaluation error. This is the same guard as in `mirror-health`.

It is listed in `base/kustomization.yaml`, so both environments get it.

### `gitops/apps/demo/base/rollout.yaml` (dev uses base directly)
- New step at index 5:
  ```yaml
  - experiment:
      duration: 3m
      templates:
        - {name: baseline, specRef: stable, replicas: 1, weight: 20, service: {name: demo-exp-baseline}}
        - {name: canary,   specRef: canary, replicas: 1, weight: 20, service: {name: demo-exp-canary}}
      analyses:
        - {name: compare, templateName: canary-vs-baseline}
  ```
  - **`specRef`** copies the pod template from the stable or canary ReplicaSet, so the
    image tag written by CI still drives everything.
  - **`weight` with `service.name`**: the controller creates the two Services and adds them
    to the VirtualService's `primary` route, splitting traffic 60 / 20 / 20 between stable,
    baseline and canary. At about 5 requests per second from loadgen, each experiment pod
    gets roughly 60 requests per 1m window.
  - **The fresh baseline is the point.** Both pods start together, so startup memory, cold
    caches and node-wide slowness affect both and cancel out in the delta.
- **`analysis.startingStep: 6 → 7`**, because the insert shifts every later step by one.
  Background `canary-health` still starts at the pause after `setWeight: 10`.

### `gitops/apps/demo/overlays/prod/kustomization.yaml`
Prod **replaces** the whole steps list, so the base step doesn't reach it. It has its own copy:
- **Experiment at index 6:** prod has an extra `pause: 2m` in the mirror phase.
- **`startingStep: 7 → 8`.**
- **Weights 10 / 10 and `duration: 5m`.** In prod every request is a real user. With 10% the
  canary is never shown to more users than the first canary step. The longer duration makes
  up for less traffic per pod.

Resulting step lists:
```
dev:  setCanaryScale, setMirrorRoute, analysis, setMirrorRoute, setCanaryScale, EXPERIMENT,
      setWeight 10, pause 1m, setWeight 50, pause 1m                       (startingStep 7)
prod: setCanaryScale, setMirrorRoute, pause 2m, analysis, setMirrorRoute, setCanaryScale,
      EXPERIMENT, setWeight 10, pause 5m, setWeight 50, pause 10m          (startingStep 8)
```

**Time per dev release:** about 1.5 min mirror + 3 min experiment + 2 min canary ≈ 7 min.

## 2. New app `demo-bg` (blue-green + Job provider)

A Rollout has one strategy, so blue-green is a second app next to `demo`. It uses the same
image and faults, and lives in its own namespace.

```
gitops/apps/demo-bg/
  base/
    namespace.yaml               demo-bg, istio-injection enabled
    peer-authentication.yaml     STRICT mTLS, as in demo
    services.yaml                demo-bg-active, demo-bg-preview
    virtual-services.yaml        bg.<env>.internal -> active, bg-preview.<env>.internal -> preview
    smoke-test.yaml              AnalysisTemplate smoke-test (Job provider)
    post-promotion-analysis.yaml AnalysisTemplate bg-post-promotion (Prometheus)
    rollout.yaml                 Rollout demo-bg, strategy blueGreen
    loadgen.yaml                 traffic to bg.<env>.internal through the gateway
    kustomization.yaml           images: newTag set BY HAND
  overlays/{dev,prod}/kustomization.yaml   hosts per env
gitops/clusters/dev/demo-bg.yaml    Argo CD Application, tracks main  (sync wave 6)
gitops/clusters/prod/demo-bg.yaml   Argo CD Application, tracks release
```

### How a release flows
```
newTag changed in Git
 -> new ReplicaSet scaled up, selected only by demo-bg-preview      (users: 0% on new)
 -> prePromotionAnalysis: smoke-test Job, 50 requests to the preview
      fail -> abort, preview scaled down, users never saw it
 -> promotion: demo-bg-active selector switched to the new hash     (users: 100% on new)
 -> postPromotionAnalysis: bg-post-promotion, about 2.5 min
      fail -> active switched back to the old ReplicaSet (still running)
 -> old ReplicaSet scaled down after scaleDownDelaySeconds: 300
```

### Why it's built this way
- **No `trafficRouting`, so the VirtualServices never change.** Promotion only switches
  Service selectors. That's why the `demo-bg` Application, unlike `demo`, needs no
  `ignoreDifferences`. Argo CD doesn't see the hash added to the selectors as a difference,
  the same as for `demo-stable` and `demo-canary` today.
- **`autoPromotionEnabled: true`:** promotion happens as soon as the smoke test passes.
  Set it to `false` if you want to look at `bg-preview.dev.internal` and run
  `kubectl argo rollouts promote demo-bg -n demo-bg` yourself.
- **`scaleDownDelaySeconds: 300`** is longer than the post-promotion analysis, so a rollback
  is an instant selector switch rather than a cold start.
- **`progressDeadlineSeconds: 120` + `progressDeadlineAbort`** catch `FAIL_READINESS` exactly as in demo.
- **The image tag is set by hand.** The demo repo's CI only writes `demo`'s `newTag`. If it
  wrote both, every release would run a canary and a blue-green rollout at the same time on one
  node, and their results would mix. Use any tag CI has already built.

### Job provider: `smoke-test.yaml`
- The metric is a Job. **Job succeeds (exit 0) means pass; Job fails means fail.** There are no
  Prometheus queries and no thresholds beyond what the script decides.
- **The script:** 50 × `curl http://demo-bg-preview/api/work`. Any non-2xx response is counted
  and printed, and the Job exits non-zero if anything failed.
- **`backoffLimit: 0`:** one attempt only. A retry would hide a flaky release.
  **`activeDeadlineSeconds: 120`** is the upper limit.
- **Mesh:** the Job pod runs inside the mesh, because the namespace has injection on and STRICT
  mTLS rejects plain-text callers. On this cluster `istio-proxy` runs as a **native sidecar**
  (an init container with `restartPolicy: Always`, checked on the demo pod). The kubelet stops it
  when curl exits, so the Job can complete. With a classic sidecar the Job would never finish.
- **It deliberately checks status codes only.** Latency and leaks go undetected here and are
  caught after promotion. That is what makes the "caught after promotion, 100% of users exposed"
  case testable.
- **0 failures out of 50 is strict:** `ERROR_RATE=2` passes only about 36% of the time
  (0.98^50). Smoke tests are for gross breakage, and that variation is worth showing in the thesis.
- **RBAC:** the Rollouts controller's ClusterRole may already create Jobs. Checked.

### Post-promotion analysis: `post-promotion-analysis.yaml`
- **Metrics:** success-rate ≥ 0.95, latency-p95 ≤ 300 ms, memory-growth ≤ 0.05 MiB/s.
  These are the same thresholds as `canary-health`.
- **Selecting the new pods:** queries use `reporter="destination"` (the new pod's own sidecar)
  with `rollouts_pod_template_hash="{{args.new-hash}}"`. Prometheus copies pod labels onto
  Istio series. This counts only requests the new version served, so the old version's traffic
  from before the switch doesn't dilute the 1m window.
- **`count` makes the analysis end:** about 150 s for success and latency, about 140 s for memory.
- **Memory `initialDelay: 60s`:** the new pod has already been running through the smoke test,
  so its startup jump is outside the window by then.

## Before testing

1. **Reconnect NetBird on the Mac.** It was disconnected when these files were written, so
   two checks are still open:
   ```sh
   kubectl --context k3s-dev create --dry-run=server -f gitops/clusters/dev/demo-bg.yaml
   curl -s https://prometheus.dev.internal/-/ready
   ```
   Already validated: kustomize renders for both apps in both envs; a server-side dry run of
   every `demo` and `demo-bg` object against the dev API server (CRD schemas and the Istio
   webhook); all 11 PromQL queries parsed with Prometheus's parser; the smoke script with `sh -n`.
2. **Review and commit.** Two commits keep them separately revertible:
   ```sh
   git add gitops/apps/demo
   git commit -m "demo: experiment step (canary vs fresh baseline), dry run"
   git add gitops/apps/demo-bg gitops/clusters/*/demo-bg.yaml docs/progressive-delivery-tests.md README.md
   git commit -m "demo-bg: blue-green twin with Job smoke test and post-promotion analysis"
   git pull && git push
   ```
3. **Refresh Argo CD.** The root app picks up `demo-bg.yaml` from `gitops/clusters/dev/`:
   ```sh
   kubectl --context k3s-dev -n argocd annotate application root argocd.argoproj.io/refresh=normal --overwrite
   kubectl --context k3s-dev -n argocd get applications demo demo-bg
   ```
   Expect both Synced and Healthy. Changing `demo`'s strategy does **not** start a canary,
   because the pod template is unchanged. `demo-bg`'s first revision comes up directly, with no
   analysis.
4. **Check `https://bg.dev.internal`.** It should answer with v4. The wildcard DNS and the
   `*.dev.internal` certificate already cover it.

## Images

| Tag | Faults | Available |
|---|---|---|
| `v2-fbb734b-3`, `v3-b355053-4`, `v4-2f61766-5` | none (healthy) | yes |
| `v5-3e32cfe-6` | `ERROR_RATE=30` | yes |
| latency, leak, readiness | `LATENCY_MS=800`, `LEAK_MB_PER_MIN=20`, `FAIL_READINESS=true` | **build** |

Building one means editing `release.env` in the demo repo and pushing. CI also points `demo`
at it, so **every build is a canary/experiment test run as well**. Note what happens, then set
`demo`'s `newTag` back to `v4-2f61766-5`.

## Test A: experiment step (`demo`)

Watch every run with:
```sh
kubectl argo rollouts get rollout demo -n demo --context k3s-dev -w
kubectl --context k3s-dev -n demo get experiment,svc,pods
kubectl --context k3s-dev -n demo get vs demo -o yaml | grep -A4 'host: demo'
```

**A0, dry run (once):** a new healthy build, for example bump `APP_VERSION` in the demo repo.
At step 5, check:
1. Services `demo-exp-baseline` and `demo-exp-canary` exist, and the VirtualService shows
   weights 60 / 20 / 20. *Not verified yet:* if both experiment pods get no traffic, the
   traffic metrics will report "no data" errors.
2. The experiment pod names match `demo-.+-canary-.*` and `demo-.+-baseline-.*`. If not, fix
   `memory-delta`.
3. **The baseline pod is not behind `demo-stable`:** compare
   `kubectl -n demo get endpoints demo-stable demo-exp-baseline`. The baseline has the same pod
   template as stable, so this is the one to check.
4. Every metric has values:
   `kubectl -n demo get analysisrun -o name | grep compare` and look at `measurements` in its YAML.
   For a healthy release, error-delta ≈ 0, latency-delta about ±10 ms, memory-delta ≈ 0.

Then **delete the `dryRun` block**, commit and push.

**A1–A4, real runs.** Log each in the Blast Radius Notebook as config E.

| Run | Release | Expected |
|---|---|---|
| A1 | healthy | experiment Successful, promoted |
| A2 | `LEAK_MB_PER_MIN=20` | mirror phase passes (it doesn't check memory); `memory-delta` fails → abort at step 5 |
| A3 | v5 `ERROR_RATE=30`, mirror phase disabled (patch below) | `error-delta` ≈ 0.30 → abort |
| A4 | healthy + CPU stress on prgdlv-test during step 5 | relative checks pass; compare with `canary-health`'s absolute `latency-p95` |

Temporary dev-only patch for A3, which removes the four mirror-phase steps. Add it to
`overlays/dev/kustomization.yaml` and delete it afterwards:
```yaml
  - target: {kind: Rollout, name: demo}
    patch: |-
      - {op: remove, path: /spec/strategy/canary/steps/0}
      - {op: remove, path: /spec/strategy/canary/steps/0}
      - {op: remove, path: /spec/strategy/canary/steps/0}
      - {op: remove, path: /spec/strategy/canary/steps/0}
      - {op: replace, path: /spec/strategy/canary/analysis/startingStep, value: 3}
```
After it, the steps are `setCanaryScale match, experiment, setWeight 10, pause, …`, so the
pause is index 3.

## Test B: blue-green + Job provider (`demo-bg`)

Change `newTag` in `gitops/apps/demo-bg/base/kustomization.yaml`, commit, push, refresh. Watch:
```sh
kubectl argo rollouts get rollout demo-bg -n demo-bg --context k3s-dev -w
kubectl --context k3s-dev -n demo-bg get jobs,pods
kubectl --context k3s-dev -n demo-bg logs job/<job-name> -c smoke    # the script's output
```

| Run | newTag | Expected | Users exposed |
|---|---|---|---|
| B1 | `v3-b355053-4` (healthy) | Job passes → promoted → post-promotion passes | 100%, healthy |
| B2 | `v5-3e32cfe-6` | Job fails (~15 of 50) → aborted before promotion | **0%** |
| B3 | latency build | Job passes (status codes only) → promoted → `latency-p95` fails → switched back | **100%** for ~1 min |
| B4 | leak build | promoted → `memory-growth` fails → switched back | 100% for ~1–1.5 min |
| B5 | readiness build | preview never Ready → abort after 120 s | 0% |

**Check on B1:**
- The Job pod shows `istio-proxy` under `initContainers`, and the Job reaches `Complete`.
  If it hangs at `1/2 Running` after curl exits, the native sidecar isn't working.
- The old ReplicaSet stays up until the post-promotion analysis has finished.
- Argo CD `demo-bg` stays Synced.

**After an abort**, Git still points at the bad tag and the rollout stays Degraded, as in demo.
Set `newTag` back to the active tag.

Blast radius for B3/B4: the 5xx or slow requests the new version served to users, from
promotion until the switch back:
```promql
sum(increase(istio_requests_total{reporter="destination", namespace="demo-bg",
  rollouts_pod_template_hash="<new hash>"}[10m]))
```
In the B runs the smoke Job's requests also count; subtract 50.

## Promoting to prod
Only after A0–A1 pass with `dryRun` removed: push `main` to `release`, as described in the
README. Prod isn't built yet; see the decision in the experiments roadmap.
