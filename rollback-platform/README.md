# rollback-platform

Two separate single-node k3s clusters (dev, prod) where a bad release is detected
from Istio traffic metrics and rolled back automatically by Argo Rollouts.
Design: [ARCHITECTURE.md](ARCHITECTURE.md).

| Env  | VM                        | Domain            | Git ref   |
|------|---------------------------|-------------------|-----------|
| dev  | prgdlv-test.netbird.cloud | `*.dev.internal`  | `main`    |
| prod | prgdlv-auto.netbird.cloud | `*.prod.internal` | `release` |

Git remote (what Argo CD pulls): `http://100.83.93.156:3000/admin/rollback-platform.git`

## Control machine prerequisites

- ansible-core 2.15+, `kubectl`, `helm`, `sops`, `age`, Python `kubernetes` in Ansible's Python
- The age key at `~/Library/Application Support/sops/age/keys.txt` (macOS default; elsewhere
  `~/.config/sops/age/keys.txt`). It is not in the repo. Without it nothing can be decrypted.
- On the NetBird network

## Build an environment

```sh
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventories/dev/hosts.yml site.yml
kubectl --context k3s-dev -n argocd get applications
```

Ansible installs k3s, creates the root CA secret, installs Argo CD and applies the root
Application for `gitops/clusters/<env>/`. Argo CD does everything else.

## Promote to prod

```sh
git push origin main:release
```

## Secrets

`ansible/inventories/<env>/group_vars/all/secrets.sops.yml` holds the k3s token and the
environment's root CA key pair. Edit with `sops <file>`. The public CA certificates are in
`docs/ca/` for installing into trust stores.

See also [docs/dns.md](docs/dns.md), [docs/rollback-test.md](docs/rollback-test.md) and
[docs/progressive-delivery-tests.md](docs/progressive-delivery-tests.md) (experiment step,
blue-green `demo-bg`, Job provider) and [docs/thesis-test-plan.md](docs/thesis-test-plan.md)
(strategy × fault test matrix and the trial runner).
