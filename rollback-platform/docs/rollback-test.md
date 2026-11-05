# Rollback test

1. In `gitops/apps/demo/base/rollout.yaml` set `PODINFO_RANDOM_ERROR` to `"true"`,
   commit and push to `main`.
2. Watch: `kubectl argo rollouts --context k3s-dev -n demo get rollout demo --watch`
3. Expected: the canary takes 10% of traffic, the `success-rate` analysis measures about
   0.67 (below 0.95), the rollout goes `Degraded` and all traffic returns to stable.
4. Evidence in Prometheus (`kubectl -n monitoring port-forward svc/prometheus-server 9090:80`):
   `sum by (response_code) (rate(istio_requests_total{destination_service_name="demo-canary"}[2m]))`
5. Revert the change in Git. The rollout returns to `Healthy` on the stable revision.
