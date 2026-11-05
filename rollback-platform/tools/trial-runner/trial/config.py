"""Loads matrix.toml and resolves paths against the repo root."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class App:
    name: str
    kind: str  # "canary" or "bluegreen"
    namespace: str
    rollout: str
    reset_tag: str
    kustomization: Path
    virtual_service: str | None = None
    active_service: str | None = None


@dataclass
class Config:
    context: str
    prometheus: str
    ca_file: Path
    poll_seconds: float
    timeout_minutes: float
    apps: dict[str, App]
    faults: dict[str, str]
    cells: list[dict]
    timeouts: dict[str, float]  # fault -> minutes, overriding timeout_minutes

    def app_for(self, strategy: str) -> App:
        return self.apps["demo-bg" if strategy == "bg" else "demo"]


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    c = raw["cluster"]
    apps = {}
    for name, a in raw["apps"].items():
        apps[name] = App(
            name=name,
            kind=a["kind"],
            namespace=a["namespace"],
            rollout=a["rollout"],
            reset_tag=a["reset_tag"],
            kustomization=REPO_ROOT / a["kustomization"],
            virtual_service=a.get("virtual_service"),
            active_service=a.get("active_service"),
        )
    return Config(
        context=c["context"],
        prometheus=c["prometheus"],
        ca_file=REPO_ROOT / c["ca_file"],
        poll_seconds=float(c.get("poll_seconds", 2)),
        timeout_minutes=float(c.get("timeout_minutes", 20)),
        apps=apps,
        faults=raw.get("faults", {}),
        cells=raw.get("cells", []),
        timeouts={k: float(v) for k, v in raw.get("timeouts", {}).items()},
    )
