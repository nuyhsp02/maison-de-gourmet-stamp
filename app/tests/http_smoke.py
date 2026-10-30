"""Real HTTP smoke/load test, intentionally restricted to localhost demo servers.

Creates 30 disposable visitor collections; point it at a separate demo database.
Usage: python tests/http_smoke.py /absolute/path/to/demo/manifest.json
"""
import http.cookiejar
import json
import statistics
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    parsed = urlsplit(manifest[0]["url"])
    assert parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    base = f"{parsed.scheme}://{parsed.netloc}"
    keys = [urlsplit(p["url"]).path.split("/")[-1] for p in manifest]
    measurements, lock = [], threading.Lock()

    def visit(index):
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        csrf = None
        def call(path, data=None):
            headers = {"Origin": base, "X-Stamp-Client": "1"}
            if csrf: headers["X-CSRF-Token"] = csrf
            payload = None
            if data is not None:
                headers["Content-Type"] = "application/json"
                payload = json.dumps(data).encode()
            started = time.perf_counter()
            with client.open(urllib.request.Request(base + path, data=payload, headers=headers), timeout=30) as response:
                assert response.status == 200
                result = json.load(response)
            with lock: measurements.append((time.perf_counter() - started) * 1000)
            return result
        assert call("/api/state")["event"]["draft"] is True
        call("/api/session", {})
        state = call("/api/state")
        csrf = state["csrf"]
        assert state["customer"]["count"] == 0
        order = keys[index % 10:] + keys[:index % 10]
        for key in order: assert call("/api/collect", {"qr": key})["added"] is True
        assert call("/api/collect", {"qr": order[0]})["added"] is False
        state = call("/api/redeem", {"confirm_received": True})
        assert state["customer"]["status"] == "redeemed"
        assert call("/api/state")["customer"]["count"] == 10
        return True

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert all(pool.map(visit, range(30)))
    elapsed = time.perf_counter() - started
    ordered = sorted(measurements)
    print(json.dumps({"visitors": 30, "concurrency": 30, "requests": len(ordered),
        "errors": 0, "elapsed_seconds": round(elapsed, 2),
        "latency_ms_p50": round(statistics.median(ordered), 2),
        "latency_ms_p95": round(ordered[int(len(ordered) * .95) - 1], 2),
        "latency_ms_max": round(max(ordered), 2)}, indent=2))


if __name__ == "__main__": main()
