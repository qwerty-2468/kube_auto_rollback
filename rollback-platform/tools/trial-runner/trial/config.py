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
    release: Path  # the env patch that defines the release
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
    reset: dict[str, str]  # env of the healthy release between trials
    faults: dict[str, dict[str, str]]  # fault -> env values that differ from reset
    cells: list[dict]
    timeouts: dict[str, float]  # fault -> minutes, overriding timeout_minutes

    def app_for(self, strategy: str) -> App:
        return self.apps["demo-bg" if strategy == "bg" else "demo"]

    def release_env(self, fault: str) -> dict[str, str]:
        return self.reset | self.faults[fault]


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
            release=REPO_ROOT / a["release"],
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
        reset={k: str(v) for k, v in raw["reset"].items()},
        faults={f: {k: str(v) for k, v in env.items()} for f, env in raw.get("faults", {}).items()},
        cells=raw.get("cells", []),
        timeouts={k: float(v) for k, v in raw.get("timeouts", {}).items()},
    )
