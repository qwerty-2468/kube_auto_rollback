# Auto-rollback test app

A small HTTP app whose faults are fixed at image build time, so every test
scenario is a real release: a new image, rolled out as a canary by
[rollback-platform](http://prgdlv-auto.netbird.cloud:3000/admin/rollback-platform).

```
edit release.env, push
  -> Gitea Actions: build image with release.env baked in, push to Gitea's registry,
     set newTag in rollback-platform/gitops/apps/demo/base/kustomization.yaml, push
  -> Argo CD (dev tracks main) syncs -> Argo Rollouts canary 10% -> 50% -> 100%
  -> AnalysisTemplate canary-health checks success rate, p95 latency, memory growth
     any check failing -> abort, all traffic back to stable
```

## Faults (release.env)

| Variable | Effect |
|---|---|
| `ERROR_RATE` | % of `/api/work` requests that return 500 |
| `LATENCY_MS` | fixed delay added to every `/api/work` request |
| `LATENCY_JITTER_MS` | extra random delay, 0..N ms |
| `FAIL_READINESS` | `true`: `/health` returns 503, pods never become Ready |
| `LEAK_MB_PER_MIN` | memory held and never freed (container limit 128Mi) |
| `WRONG_ANSWER` | `true`: `/api/work` returns 200 with `result` 5051 instead of 5050 |
| `APP_VERSION`, `COLOR` | label and page colour, to tell versions apart |

Faults apply to `/api/work` only. `/` (page), `/stats` (config and counters)
and `/metrics` keep working.

## Scenarios and what should happen

| release.env | Expected | Caught by |
|---|---|---|
| all faults 0 | promoted to 100% | nothing fails |
| `ERROR_RATE=30` | aborted after ~1 min | success-rate < 0.95 |
| `ERROR_RATE=2` | promoted (98% >= 95%) | shows the threshold |
| `LATENCY_MS=800` | aborted | latency-p95 > 300 ms |
| `LATENCY_JITTER_MS=1500` | aborted | latency-p95 |
| `LATENCY_MS=150` | promoted | shows the threshold |
| `FAIL_READINESS=true` | aborted after 2 min, canary never gets traffic | progress deadline |
| `LEAK_MB_PER_MIN=20` | aborted at ~1.5 min, before any OOMKill | memory-growth > 0.05 MiB/s |
| `WRONG_ANSWER=true` | promoted by every canary strategy; blue-green's smoke Job aborts it | the smoke Job's body check only |

## Recovering after an aborted release

Git (rollback-platform) still points at the bad tag, so the Rollout stays
`Degraded`. Either revert the `demo: release ...` commit in rollback-platform
(the previous image comes back instantly, no canary), or ship a fixed release
by setting the fault back to 0 here and pushing.

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
