"""Changes the platform the GitOps way: edit a file, commit, push, refresh Argo CD.

Argo CD self-heals, so changing the cluster directly would be reverted; every
trial is a commit instead, which also makes each run traceable to its config.
"""

import re
import subprocess
import time
from pathlib import Path

from .config import REPO_ROOT, App

DEV_OVERLAY = REPO_ROOT / "gitops/apps/demo/overlays/dev/kustomization.yaml"
COMPONENTS = REPO_ROOT / "gitops/apps/demo/components"
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


class GitError(RuntimeError):
    pass


def _git(*args) -> str:
    out = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def _replace(path: Path, pattern: str, repl: str) -> bool:
    """Regex-replace in a file; True if the file changed."""
    old = path.read_text()
    new, n = re.subn(pattern, repl, old, count=1, flags=re.M)
    if n == 0:
        raise GitError(f"{pattern!r} not found in {path}")
    path.write_text(new)
    return new != old


def component_dir(strategy: str) -> str:
    """s4 -> s4-mirror-canary"""
    found = [d.name for d in COMPONENTS.iterdir() if d.name.startswith(f"{strategy}-")]
    if len(found) != 1:
        raise GitError(f"no single component for {strategy}: {found}")
    return found[0]


def set_strategy(strategy: str) -> bool:
    return _replace(DEV_OVERLAY, r"(\.\./\.\./components/)s\d-[a-z-]+", rf"\g<1>{component_dir(strategy)}")


def set_tag(app: App, tag: str) -> bool:
    return _replace(app.kustomization, r"^(\s*newTag:).*$", rf"\g<1> {tag}")


def current_tag(app: App) -> str:
    return re.search(r"^\s*newTag:\s*(\S+)", app.kustomization.read_text(), re.M).group(1)


def commit_push(paths: list[Path], message: str) -> str:
    """Commits the given files, rebases onto CI's commits, pushes. Returns the commit."""
    _git("add", *map(str, paths))
    _git("commit", "-q", "-m", message, "-m", TRAILER)
    for attempt in range(4):  # NetBird drops for a few seconds now and then
        try:
            _git("pull", "-q", "--rebase", "--autostash")
            _git("push", "-q")
            return _git("rev-parse", "--short", "HEAD")
        except GitError:
            if attempt == 3:
                raise
            time.sleep(20)
    raise AssertionError("unreachable")


def refresh(context: str, application: str) -> None:
    subprocess.run(["kubectl", "--context", context, "-n", "argocd", "annotate", "application",
                    application, "argocd.argoproj.io/refresh=normal", "--overwrite"],
                   capture_output=True, text=True)


def wait_synced(context: str, application: str, commit: str, timeout_s: float = 300) -> None:
    """Until Argo CD has synced the app to `commit` (or a later one)."""
    full = _git("rev-parse", commit)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = subprocess.run(["kubectl", "--context", context, "-n", "argocd", "get", "application",
                              application, "-o", "jsonpath={.status.sync.status} {.status.sync.revision}"],
                             capture_output=True, text=True).stdout.split()
        if len(out) == 2 and out[0] == "Synced":
            # Synced to our commit, or to one that contains it (CI may have pushed after us).
            subprocess.run(["git", "-C", str(REPO_ROOT), "fetch", "-q"], capture_output=True)
            if subprocess.run(["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", full, out[1]],
                              capture_output=True).returncode == 0:
                return
        time.sleep(5)
    raise GitError(f"{application} not synced to {commit} after {timeout_s:.0f}s")
