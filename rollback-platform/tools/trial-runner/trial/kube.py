"""Read-only access to the cluster through kubectl."""

import json
import subprocess
import time

# One call per poll: everything the timeline needs from the app's namespace.
KINDS = "rollout,analysisrun,experiment,virtualservice,service,pod"
# NetBird drops for a few seconds now and then (DNS lookups fail). Ride out
# short gaps; a longer outage still stops the run instead of recording bad data.
RETRY_FOR_S = 90
RETRY_EVERY_S = 5


class KubeError(RuntimeError):
    pass


def _get(context: str, namespace: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["kubectl", "--context", context, "-n", namespace, "get", KINDS, "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 1, "", "kubectl timed out")


def get_all(context: str, namespace: str) -> dict[str, list[dict]]:
    """Returns the namespace's objects grouped by kind."""
    give_up = time.time() + RETRY_FOR_S
    while (out := _get(context, namespace)).returncode != 0:
        if time.time() > give_up:
            raise KubeError(out.stderr.strip())
        time.sleep(RETRY_EVERY_S)
    grouped: dict[str, list[dict]] = {}
    for item in json.loads(out.stdout)["items"]:
        grouped.setdefault(item["kind"], []).append(item)
    return grouped
