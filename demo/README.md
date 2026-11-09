# Auto-rollback test app

A small HTTP app whose faults are read from env vars at startup. There is one
healthy image; a test scenario is a change of those env vars in
[rollback-platform](http://prgdlv-auto.netbird.cloud:3000/admin/rollback-platform)
(`gitops/apps/<app>/base/release.yaml`). That changes the Rollout's pod
template, so Argo Rollouts treats it as a new release, exactly like a new image.

```
edit release.yaml in rollback-platform, push (the trial runner does this)
  -> Argo CD (dev tracks main) syncs -> Argo Rollouts: mirror, experiment, canary 10% -> 50% -> 100%
  -> the AnalysisTemplates check success rate, p95 latency, memory growth
     any check failing -> abort, all traffic back to stable
```

A code change goes through this repo instead: edit, push, and Gitea Actions
builds the image, pushes it to Gitea's registry and sets `newTag` in
rollback-platform's `gitops/apps/demo/base/kustomization.yaml`.

## Faults (env vars in release.yaml)

| Variable | Effect |
|---|---|
| `ERROR_RATE` | % of `/api/work` requests that return 500 |
| `LATENCY_MS` | fixed delay added to every `/api/work` request |
| `LATENCY_JITTER_MS` | extra random delay, 0..N ms |
| `FAIL_READINESS` | `true`: `/health` returns 503, pods never become Ready |
| `LEAK_MB_PER_MIN` | memory held and never freed (container limit 128Mi) |
| `WRONG_ANSWER` | `true`: `/api/work` returns 200 with `result` 5051 instead of 5050 |
| `APP_VERSION` | label of the release; must change, or a healthy release changes nothing |
| `COLOR` | page colour, set at build time from `release.env` |

Faults apply to `/api/work` only. `/` (page), `/stats` (config and counters)
and `/metrics` keep working.

## Scenarios and what should happen

| Fault setting | Expected | Caught by |
|---|---|---|
| all faults 0 | promoted to 100% | nothing fails |
| `ERROR_RATE=30` | aborted after ~1 min | success-rate < 0.95 |
| `ERROR_RATE=2` | promoted (98% >= 95%) | shows the threshold |
| `LATENCY_MS=800` | aborted | latency-p95 > 300 ms |
| `LATENCY_JITTER_MS=1500` | aborted | latency-p95 |
| `LATENCY_MS=150` | promoted | shows the threshold |
| `FAIL_READINESS=true` | aborted after 2 min, canary never gets traffic; hangs instead if an old ReplicaSet kept a `scale-down-deadline` annotation (argo-rollouts #4971) | progress deadline |
| `LEAK_MB_PER_MIN=20` | aborted at ~1.5 min, before any OOMKill | memory-growth > 0.05 MiB/s |
| `WRONG_ANSWER=true` | promoted by every canary strategy; blue-green's smoke Job aborts it | the smoke Job's body check only |

## Recovering after an aborted release

Git (rollback-platform) still points at the bad release, so the Rollout stays
`Degraded`. Revert the commit that changed `release.yaml` (the previous
version comes back instantly, no canary); the trial runner does this itself.

## Setup

- Repo secret `REGISTRY_TOKEN`: a Gitea access token with `write:package`
  (push the image) and `write:repository` (push to rollback-platform).
- Runner: act_runner with Docker, label `ubuntu-latest`.
- The image is public in Gitea's registry; k3s is allowed to pull it over plain
  HTTP by `registries_config_yaml` in rollback-platform's inventories.

## Run locally

```sh
ERROR_RATE=30 LATENCY_MS=200 python3 app.py   # then: curl localhost:8080/api/work
```
