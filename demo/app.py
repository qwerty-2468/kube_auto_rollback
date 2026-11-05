import json, os, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Every fault is fixed at image build time (see Dockerfile), so each test
# scenario is a separate image tag, like a real release.
VERSION = os.getenv("APP_VERSION", "v1")
COLOR = os.getenv("COLOR", "#2563eb")
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))  # % of /api/work calls that return 500
LATENCY_MS = float(os.getenv("LATENCY_MS", "0"))  # fixed delay added to /api/work
LATENCY_JITTER_MS = float(os.getenv("LATENCY_JITTER_MS", "0"))  # extra random 0..N ms on /api/work
FAIL_READINESS = os.getenv("FAIL_READINESS", "false").lower() == "true"  # /health returns 503
LEAK_MB_PER_MIN = float(os.getenv("LEAK_MB_PER_MIN", "0"))  # memory held and never freed
WRONG_ANSWER = os.getenv("WRONG_ANSWER", "false").lower() == "true"  # /api/work answers 200 with a wrong result

lock = threading.Lock()
stats = {"total": 0, "errors": 0}
leaked = []

CONFIG = {"version": VERSION, "error_rate": ERROR_RATE, "latency_ms": LATENCY_MS,
          "latency_jitter_ms": LATENCY_JITTER_MS, "fail_readiness": FAIL_READINESS,
          "leak_mb_per_min": LEAK_MB_PER_MIN, "wrong_answer": WRONG_ANSWER}

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>canary demo</title>
<style>body{font-family:system-ui;background:%(c)s;color:#fff;text-align:center;padding-top:15vh}
code{background:#0003;padding:2px 8px;border-radius:4px}</style></head><body>
<h1>Version: %(v)s</h1><p><code>%(cfg)s</code></p>
<p id=s>calling /api/work ...</p>
<script>setInterval(async()=>{const t=performance.now();const r=await fetch('/api/work');
document.getElementById('s').textContent='/api/work -> '+r.status+' in '+Math.round(performance.now()-t)+' ms'},1000)</script></body></html>"""


def leak_memory():
    # Every 6s keep another tenth of the per-minute amount. b"x" * n writes the
    # bytes, so the pages really count as used memory (a zeroed buffer may not).
    chunk = int(LEAK_MB_PER_MIN * 1024 * 1024 / 10)
    while True:
        time.sleep(6)
        leaked.append(b"x" * chunk)


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            cfg = ", ".join(f"{k}={v}" for k, v in CONFIG.items() if k != "version")
            self._send(200, PAGE % {"c": COLOR, "v": VERSION, "cfg": cfg}, "text/html")
        elif self.path == "/health":
            if FAIL_READINESS:
                self._send(503, '{"status":"failing on purpose"}')
            else:
                self._send(200, '{"status":"ok"}')
        elif self.path == "/api/work":
            delay = LATENCY_MS + random.uniform(0, LATENCY_JITTER_MS)
            if delay:
                time.sleep(delay / 1000)
            fail = random.random() * 100 < ERROR_RATE
            with lock:
                stats["total"] += 1
                stats["errors"] += fail
            # The "work": sum of 1..100. WRONG_ANSWER is an off-by-one bug: still 200,
            # still fast, just a wrong number, so no status or latency metric sees it.
            result = sum(range(1, 101)) + (1 if WRONG_ANSWER else 0)
            self._send(500 if fail else 200,
                       json.dumps({"version": VERSION, "ok": not fail, "delay_ms": round(delay),
                                   "result": result}))
        elif self.path == "/stats":
            with lock:
                t, e = stats["total"], stats["errors"]
            self._send(200, json.dumps({**CONFIG, "total": t, "errors": e,
                                        "observed_error_rate": round(100 * e / t, 2) if t else 0,
                                        "leaked_mb": round(sum(map(len, leaked)) / 2**20, 1)}))
        elif self.path == "/metrics":  # Prometheus format, if you prefer a prometheus provider
            with lock:
                t, e = stats["total"], stats["errors"]
            self._send(200, f'http_requests_total{{version="{VERSION}",code="2xx"}} {t-e}\n'
                            f'http_requests_total{{version="{VERSION}",code="5xx"}} {e}\n', "text/plain")
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass  # no per-request access log: loadgen sends several requests a second


if __name__ == "__main__":
    if LEAK_MB_PER_MIN > 0:
        threading.Thread(target=leak_memory, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
