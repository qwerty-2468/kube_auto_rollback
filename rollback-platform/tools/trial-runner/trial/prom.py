"""Prometheus HTTP API (instant queries only)."""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .kube import RETRY_EVERY_S, RETRY_FOR_S


class Prometheus:
    def __init__(self, url: str, ca_file: Path):
        self.url = url.rstrip("/")
        # The internal CA isn't in Python's trust store, so trust it explicitly.
        self.ctx = ssl.create_default_context(cafile=str(ca_file))

    def query(self, expr: str, at: float | None = None) -> dict:
        """The response's data: {"resultType": ..., "result": ...}."""
        params = {"query": expr}
        if at is not None:
            params["time"] = f"{at:.3f}"
        url = f"{self.url}/api/v1/query?{urllib.parse.urlencode(params)}"
        # Same short-outage tolerance as kube.get_all (NetBird DNS drops).
        give_up = time.time() + RETRY_FOR_S
        while True:
            try:
                with urllib.request.urlopen(url, context=self.ctx, timeout=30) as r:
                    body = json.load(r)
                break
            except urllib.error.URLError:
                if time.time() > give_up:
                    raise
                time.sleep(RETRY_EVERY_S)
        if body["status"] != "success":
            raise RuntimeError(f"{expr}: {body.get('error')}")
        return body["data"]

    def scalar(self, expr: str, at: float | None = None) -> float:
        """Value of a query that returns one number; 0 when it returns no series."""
        data = self.query(expr, at)
        result = data["result"]
        if data["resultType"] == "scalar":  # e.g. time(): [timestamp, "value"]
            return float(result[1])
        return float(result[0]["value"][1]) if result else 0.0

    def server_time(self) -> float:
        return self.scalar("time()")
