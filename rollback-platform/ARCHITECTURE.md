# Architecture: automated rollback on Kubernetes (dev + prod)

Spec for building this repo. Section 12 records the decisions that replaced the original open questions.

## 1. Goal

Build, from zero and repeatably, two fully separate Kubernetes environments (dev and prod) on two small VMs. The point of the project is **automated rollback**: a bad release is detected from live traffic metrics and reverted without a human.

Principles:
- Everything is code. A VM can be wiped and rebuilt with a couple of commands.
- Ansible configures hosts and bootstraps the cluster. Argo CD (GitOps) owns everything inside the cluster afterwards.
- Dev and prod are independent clusters built from the same code. Only variables and Git refs differ.

## 2. Constraints

| Item | Value |
|---|---|
| Hardware | 2 VMs, 8 GB RAM and 4 CPUs each |
| Layout | VM1 = dev, VM2 = prod, one single-node cluster each, fully separate |
| Access | VMs are not publicly reachable. Access is only over a NetBird VPN, which is already installed (no role needed) |
| Domain | No public domain. Private names under `.internal`, configured on NetBird DNS |
| Service mesh / ingress | Istio (its ingress gateway is the only ingress; no NGINX or Traefik) |
| TLS | cert-manager with its own CA issuer |
| Provisioning | Ansible. No `base_os` role (dropped on purpose) |

No high availability: a single node per environment. Automated rollback protects against bad releases, not against VM loss. Recovery from VM loss = rebuild with Ansible and let Argo CD resync.

## 3. Target architecture (per environment)

```
 your machine (on NetBird)
        │  https://app.<env>.internal
        ▼
 VM (k3s single node)
 ┌────────────────────────────────────────────────────────┐
 │ Istio ingress gateway (Envoy, ports 80/443 via         │
 │ k3s ServiceLB)  ── TLS: wildcard cert from cert-manager│
 │        │                                               │
 │ Gateway + VirtualService (weights set by Rollouts)     │
 │        ├── stable Service ── stable pods (sidecars)    │
 │        └── canary Service ── canary pods (sidecars)    │
 │                                                        │
 │ Argo CD · Argo Rollouts · cert-manager · istiod        │
 │ Prometheus (scrapes Envoy metrics)                     │
 └────────────────────────────────────────────────────────┘
```

Rollback loop: Argo CD syncs a new image tag, Argo Rollouts shifts a small share of traffic to the canary through the Istio VirtualService, an AnalysisTemplate queries Prometheus (Istio request metrics), and if the error rate exceeds the threshold the rollout aborts and traffic returns to stable.

## 4. Tooling and versions

Control machine (where Ansible is run, on the NetBird network, not one of the VMs): ansible-core 2.15 or newer, `kubectl`, `helm`, `sops`, `age`, Python `kubernetes` library.

Pinned versions:

| Component | Version |
|---|---|
| k3s | v1.36.5+k3s1 |
| k3s-ansible collection | 1.2.2 |
| kubernetes.core / community.sops / ansible.utils | 6.4.0 / 2.3.0 / 6.0.2 |
| Argo CD chart | 10.10.3 (Argo CD v3.5.5) |
| cert-manager chart | v1.21.2 |
| Istio charts (`base`, `istiod`, `gateway`) | 1.30.6 |
| prometheus chart | 29.37.0 |
| Argo Rollouts chart | 2.43.7 |
| Grafana chart (grafana-community) | 13.7.0 (Grafana 13.2.4) |
| Kiali server chart | 2.33.0 |
| podinfo (demo app) | 6.15.0 |

## 5. Repository layout

```
.
├── ARCHITECTURE.md
├── ansible/
│   ├── ansible.cfg
│   ├── requirements.yml
│   ├── site.yml                      # imports the playbooks below in order
│   ├── playbooks/
│   │   ├── 01-k3s.yml                # runs the k3s.orchestration collection
│   │   └── 02-bootstrap.yml          # runs the argocd_bootstrap role
│   ├── inventories/{dev,prod}/
│   │   ├── hosts.yml
│   │   └── group_vars/all/{main.yml,secrets.sops.yml}
│   └── roles/argocd_bootstrap/
├── gitops/
│   ├── clusters/{dev,prod}/          # Argo CD Applications (same set, different Git ref)
│   ├── platform/
│   │   ├── cert-manager/             # chart values
│   │   ├── istio/                    # istiod and gateway values
│   │   ├── networking/               # ClusterIssuer, Certificate, Gateway (kustomize overlays per env)
│   │   ├── monitoring/               # values.yaml + values-<env>.yaml
│   │   ├── grafana/                  # data source, Istio dashboards, Rollback dashboard
│   │   ├── kiali/
│   │   └── rollouts/
│   └── apps/demo/{base,overlays/{dev,prod}}
└── docs/                             # dns.md, rollback-test.md, ca/<env>-root-ca.crt
```

Secrets are SOPS-encrypted with an age key kept outside the repo. The `community.sops` vars plugin decrypts `*.sops.yml` under `group_vars/` when Ansible runs.

## 6. Ansible layer

Two steps only.

### 6.1 k3s (`playbooks/01-k3s.yml`)

Runs `k3s.orchestration.site`. One host in `server`, no `agent` hosts, so it creates a single-node cluster. `--disable traefik` (Istio is the ingress); ServiceLB stays enabled because it exposes the Istio gateway on ports 80/443 of the node. The cluster token comes from SOPS. The kubeconfig is merged into the control machine's `~/.kube/config` under the context `k3s-dev` or `k3s-prod`.

### 6.2 `argocd_bootstrap` role (`playbooks/02-bootstrap.yml`)

Runs on the control machine with `kubernetes.core`:
1. Create namespaces `argocd`, `cert-manager`, `istio-system`, `monitoring`.
2. Create the secrets that come from SOPS: the root CA `internal-root-ca` in `cert-manager`, and `grafana-admin` in `monitoring`. For the CA: (type `kubernetes.io/tls`) in `cert-manager` from SOPS. Argo CD cannot decrypt SOPS, and keeping the CA in SOPS keeps it stable across rebuilds. One CA per environment.
3. Install Argo CD by Helm (dex and notifications disabled, single replicas, resource limits, and a health check for `Application` so sync waves wait for each child app).
4. Apply the root `Application` pointing at `gitops/clusters/<env>/`. Dev tracks `main`, prod tracks `release`.

After step 4, Ansible is done. Everything else is GitOps.

### 6.3 Host prerequisites (manual, outside Ansible)

SSH access with sudo for `ubuntu`, NetBird running, Ubuntu 24.04. Optional tuning only if problems appear: raise `fs.inotify.max_user_watches` / `max_user_instances` on "too many open files". k3s tolerates swap.

## 7. GitOps layer

| Wave | Application | Notes |
|---|---|---|
| 0 | cert-manager | CRDs enabled |
| 1 | istio-base | CRDs |
| 2 | istiod | single replica, no HPA, resource limits |
| 3 | istio-ingressgateway, networking | gateway chart; ClusterIssuer + Certificate + Gateway |
| 4 | prometheus, grafana, kiali | standalone Prometheus; Grafana and Kiali read from it |
| 5 | argo-rollouts | |
| 6 | demo | Rollout, AnalysisTemplate, VirtualService, load generator |

Helm Applications are multi-source: the chart from its repo plus `$values` files from this repo. Plain-manifest Applications use `SkipDryRunOnMissingResource=true`. Argo CD ignores webhook fields that istiod rewrites, and the VirtualService route that Argo Rollouts reweights.

### 7.1 Istio

- Ingress gateway is its own Helm release `istio-ingressgateway` in `istio-system`, pod label `istio: ingressgateway`.
- Sidecar injection only on app namespaces (`demo`). Never on `argocd`, `monitoring`, `cert-manager`.
- Sidecar mode, not ambient.
- `PeerAuthentication` `STRICT` in `demo`.
- `meshConfig.enablePrometheusMerge: false`: Prometheus scrapes Envoy directly on 15090.

### 7.2 TLS

In `istio-system`: `ClusterIssuer internal-ca` (type `ca`, secret `internal-root-ca`), `Certificate gateway-wildcard` for `<env>.internal` and `*.<env>.internal` (90 days, renew 30 days before expiry, secret `gateway-wildcard-tls`), and `Gateway main-gateway` (80 redirects to HTTPS, 443 `SIMPLE` with `credentialName: gateway-wildcard-tls`).

### 7.3 DNS

Outside this repo, on NetBird: `*.dev.internal` → VM1, `*.prod.internal` → VM2. See `docs/dns.md`. The CA certificates in `docs/ca/` are installed into trust stores by hand.

### 7.4 Argo Rollouts and automated rollback

- `demo-stable` and `demo-canary` Services, one `VirtualService` (route `primary`) on `main-gateway`, host `app.<env>.internal`.
- `Rollout` with canary steps 10% → pause → 50% → pause → 100% (dev 1m pauses, prod 5m/10m) and background analysis from the first step.
- `AnalysisTemplate success-rate`: share of non-5xx `istio_requests_total` (reporter `source`, i.e. as seen by the gateway) for `demo-canary` over 2m, must be ≥ 0.95; one failed measurement aborts the rollout and returns all traffic to stable.
- A small `loadgen` Deployment (no sidecar) sends steady traffic through the ingress gateway so the analysis always has data.
- A bad release is simulated with `PODINFO_RANDOM_ERROR=true` (1 in 3 requests fail). See `docs/rollback-test.md`.

## 8. Resource budget (per VM, 8 GB)

| Component | Approx. RAM |
|---|---|
| OS | 0.5 GB |
| k3s | 1 GB |
| istiod | 0.5 to 1 GB |
| Istio ingress gateway | 0.1 to 0.2 GB |
| Argo CD + Rollouts | 0.7 to 1 GB |
| Prometheus | 0.5 to 1 GB |
| Grafana + Kiali | about 0.3 GB |
| Sidecars | 50 to 100 MB per pod |
| Left for apps | about 2 to 3 GB |

Every platform component has explicit requests and limits. Prometheus runs without alertmanager, pushgateway, node-exporter and kube-state-metrics; retention 24h in dev, 7d in prod. Grafana (anonymous viewers, admin password from SOPS, dashboards provisioned from Git, no volume) and Kiali (anonymous, view-only) are included; together about 0.3 GB. One replica for everything.

## 9. Environment differences

| | dev | prod |
|---|---|---|
| VM | prgdlv-test (100.83.52.191) | prgdlv-auto (100.83.93.156) |
| Domain | `*.dev.internal` | `*.prod.internal` |
| Git ref tracked | `main` | `release` (promoted by hand) |
| Rollout pauses | 1m, 1m | 5m, 10m |
| Prometheus retention / volume | 24h / 2Gi | 7d / 8Gi |

Promotion flow: change lands on `main`, deploys to dev, is verified there (including a deliberate bad release), then `main` is pushed to `release` for prod.

## 10. Build order

1. Repo skeleton, `requirements.yml`, `ansible.cfg`, dev and prod inventories.
2. `01-k3s.yml` on dev. Verify `kubectl get nodes`, Traefik absent, ServiceLB working.
3. `argocd_bootstrap` on dev. Verify Argo CD is healthy and the root app syncs.
4. GitOps platform in wave order. Verify `curl https://app.dev.internal` works without `-k` once the CA is trusted.
5. Prometheus, Argo Rollouts, demo app.
6. Rollback test: a release returning 5xx must be aborted while stable keeps serving.
7. Rebuild dev from zero to prove repeatability.
8. Repeat for prod.

## 11. Acceptance criteria

- `ansible-playbook -i inventories/<env>/hosts.yml site.yml` takes a fresh VM to a working cluster with Argo CD syncing.
- `https://app.<env>.internal` serves the demo app with a valid certificate from the environment's CA.
- A faulty release is rolled back automatically with no manual step, visible in `kubectl argo rollouts get rollout` and in Prometheus.
- Wiping and rebuilding an environment leaves the trusted CA unchanged.
- No plaintext secrets in Git.

## 12. Decisions (were open questions)

- VMs run Ubuntu 24.04, SSH user `ubuntu`. Dev = prgdlv-test, prod = prgdlv-auto.
- Git remote: the existing host-level Gitea on prgdlv-auto, `http://100.83.93.156:3000/admin/rollback-platform.git` (public repo, so Argo CD needs no credentials). Addressed by NetBird IP because pods may not resolve `*.netbird.cloud`. The prod VM also hosts Gitea, so dev depends on that VM being up.
- Demo app: podinfo. No real application yet.
- Versions: see section 4.

## 13. Observability UIs

All behind `main-gateway` with the wildcard certificate, reachable only over NetBird:

| URL | What | Login |
|---|---|---|
| `argocd.<env>.internal` | Argo CD | `admin`, password in `argocd-initial-admin-secret` |
| `rollouts.<env>.internal` | Argo Rollouts dashboard | none (can promote/abort) |
| `prometheus.<env>.internal` | Prometheus | none |
| `grafana.<env>.internal` | Grafana: Rollback dashboard + Istio dashboards | anonymous Viewer; `admin` from SOPS |
| `kiali.<env>.internal` | Kiali mesh graph | anonymous, view-only |
