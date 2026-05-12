"""Buggy App — Python version.
Fault-injectable HTTP server for self-healing agent demos.
Exposes Prometheus metrics and fault injection endpoints.

Run: python main.py  (default port 3000)
"""
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ── Prometheus metrics ─────────────────────────────────────────────────────────

http_requests_total = Counter(
    "http_requests_total", "Total HTTP requests",
    ["method", "route", "status_code"],
)
http_request_duration = Histogram(
    "http_request_duration_ms", "HTTP request duration in milliseconds",
    ["method", "route"],
    buckets=[10, 50, 100, 200, 500, 1000, 3000],
)
error_rate_gauge    = Gauge("app_error_rate",       "Current application error rate (0–1)")
active_requests     = Gauge("app_active_requests",  "Active in-flight requests")
fault_active_gauge  = Gauge("app_fault_active",     "Whether a fault is active (1=yes)", ["fault"])
cpu_usage_gauge     = Gauge("app_cpu_usage_percent",    "Process-group CPU usage percent (0–100)")
memory_usage_gauge  = Gauge("app_memory_usage_percent", "Process-group memory usage percent (0–100)")

# ── Fault state ────────────────────────────────────────────────────────────────

_lock          = threading.Lock()
_state: dict   = {
    "error_mode":   False,
    "cpu_spike":    False,
    "memory_leak":  False,
    "high_latency": False,
    "cascade":      False,
    "error_rate":   0.0,
    "latency_ms":   0,
    "cpu_workers":  0,
    "leaked_chunks": 0,
}
_cpu_processes: list[subprocess.Popen] = []
_leaked_blocks:   list[bytes]           = []
_leak_stop:       threading.Event | None = None
_start_time = time.time()

# Request counters for error rate calculation
_total_requests = 0
_error_requests = 0
_stats_lock = threading.Lock()


def _update_resource_metrics():
    import psutil, os, signal
    proc = psutil.Process(os.getpid())
    while True:
        try:
            # Include child subprocesses (CPU burn workers)
            children = proc.children(recursive=True)
            all_procs = [proc] + children
            cpu = sum(p.cpu_percent(interval=None) for p in all_procs)
            mem = proc.memory_percent()
            cpu_usage_gauge.set(cpu)
            memory_usage_gauge.set(mem)
        except Exception:
            pass
        time.sleep(3)

threading.Thread(target=_update_resource_metrics, daemon=True).start()

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.before_request
def _before():
    request._start = time.time()
    active_requests.inc()


@app.after_request
def _after(response):
    active_requests.dec()
    duration_ms = (time.time() - request._start) * 1000
    route = request.path
    http_requests_total.labels(
        method=request.method,
        route=route,
        status_code=str(response.status_code),
    ).inc()
    http_request_duration.labels(method=request.method, route=route).observe(duration_ms)

    global _total_requests, _error_requests
    with _stats_lock:
        _total_requests += 1
        if response.status_code >= 500:
            _error_requests += 1
        rate = _error_requests / _total_requests if _total_requests else 0.0
    error_rate_gauge.set(rate)
    return response


# ── Health ─────────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    with _lock:
        unhealthy = _state["error_mode"] or _state["cascade"]
    if unhealthy:
        return jsonify({"status": "unhealthy", "reason": "fault_active"}), 500
    return jsonify({
        "status": "healthy",
        "uptime": f"{time.time() - _start_time:.0f}s",
    })


# ── Business endpoint ──────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    with _lock:
        latency    = _state["latency_ms"]
        err_rate   = _state["error_rate"]
        cascade    = _state["cascade"]
        hi_latency = _state["high_latency"]

    if hi_latency or cascade:
        time.sleep((latency + random.randint(0, 100)) / (1000 if latency > 0 else 1))
    else:
        time.sleep(random.uniform(0.010, 0.050))

    err_prob = 0.9 if cascade else err_rate
    if err_prob > 0 and random.random() < err_prob:
        app.logger.error(f"request failed route=/api/data reason=fault_injection")
        return jsonify({"error": "simulated fault"}), 500

    return jsonify({
        "data": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ── Fault status ───────────────────────────────────────────────────────────────

@app.route("/fault/status")
def fault_status():
    import psutil
    with _lock:
        s = dict(_state)
        workers   = len(_cpu_processes)
        leaked_mb = len(_leaked_blocks) * 20
    return jsonify({
        "faults":      s,
        "cpu_workers": workers,
        "leaked_mb":   leaked_mb,
        "uptime_sec":  int(time.time() - _start_time),
        "threads":     threading.active_count(),
    })


# ── Fault injectors ────────────────────────────────────────────────────────────

@app.route("/fault/errors", methods=["POST"])
def fault_errors():
    data = request.get_json(silent=True) or {}
    rate = float(data.get("rate", 0.8))
    with _lock:
        _state["error_mode"] = True
        _state["error_rate"] = rate
    fault_active_gauge.labels(fault="error_mode").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=error_mode error_rate={rate}")
    return jsonify({"fault": "error_mode", "status": "active", "error_rate": rate})


@app.route("/fault/cpu", methods=["POST"])
def fault_cpu():
    with _lock:
        if _state["cpu_spike"]:
            return jsonify({"fault": "cpu_spike", "status": "already_active",
                            "workers": len(_cpu_processes)})

    num_workers = min(os.cpu_count() or 2, 4)

    # Use subprocesses — threads can't bypass the GIL for real CPU pressure
    burn_code = "import math, random\nwhile True: math.sqrt(random.random())"
    for _ in range(num_workers):
        p = subprocess.Popen([sys.executable, "-c", burn_code])
        _cpu_processes.append(p)

    with _lock:
        _state["cpu_spike"]   = True
        _state["cpu_workers"] = num_workers
    fault_active_gauge.labels(fault="cpu_spike").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=cpu_spike workers={num_workers}")
    return jsonify({"fault": "cpu_spike", "status": "active", "workers": num_workers})


@app.route("/fault/memory", methods=["POST"])
def fault_memory():
    global _leak_stop
    with _lock:
        if _state["memory_leak"]:
            return jsonify({"fault": "memory_leak", "status": "already_active"})
        _state["memory_leak"] = True

    data        = request.get_json(silent=True) or {}
    mb_per_tick = int(data.get("mb_per_tick", 20))
    interval_s  = int(data.get("interval_ms", 2000)) / 1000

    _leak_stop = threading.Event()

    def _leak(stop: threading.Event):
        while not stop.is_set():
            _leaked_blocks.append(b"\x00" * (mb_per_tick * 1024 * 1024))
            with _lock:
                _state["leaked_chunks"] = len(_leaked_blocks)
            app.logger.warning(f"MEMORY_LEAK leaked_chunks={len(_leaked_blocks)}")
            time.sleep(interval_s)

    threading.Thread(target=_leak, args=(_leak_stop,), daemon=True).start()
    fault_active_gauge.labels(fault="memory_leak").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=memory_leak mb_per_tick={mb_per_tick}")
    return jsonify({"fault": "memory_leak", "status": "active",
                    "mb_per_tick": mb_per_tick, "interval_ms": int(interval_s * 1000)})


@app.route("/fault/latency", methods=["POST"])
def fault_latency():
    data = request.get_json(silent=True) or {}
    ms = int(data.get("ms", 600))
    with _lock:
        _state["high_latency"] = True
        _state["latency_ms"]   = ms
    fault_active_gauge.labels(fault="high_latency").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=high_latency latency_ms={ms}")
    return jsonify({"fault": "high_latency", "status": "active", "latency_ms": ms})


@app.route("/fault/cascade", methods=["POST"])
def fault_cascade():
    with _lock:
        _state.update({
            "cascade":      True,
            "error_mode":   True,
            "high_latency": True,
            "error_rate":   0.7,
            "latency_ms":   800,
        })
    for f in ("cascade", "error_mode", "high_latency"):
        fault_active_gauge.labels(fault=f).set(1)
    app.logger.error("FAULT_INJECTED fault=cascade error_rate=0.7 latency_ms=800")
    return jsonify({"fault": "cascade", "status": "active", "error_rate": 0.7, "latency_ms": 800})


@app.route("/fault/reset", methods=["POST"])
def fault_reset():
    global _leak_stop, _total_requests, _error_requests

    # Stop CPU workers
    for p in _cpu_processes:
        p.terminate()
    _cpu_processes.clear()

    # Stop memory leak
    if _leak_stop:
        _leak_stop.set()
        _leak_stop = None
    _leaked_blocks.clear()

    # Reset fault state
    with _lock:
        _state.update({
            "error_mode": False, "cpu_spike": False, "memory_leak": False,
            "high_latency": False, "cascade": False,
            "error_rate": 0.0, "latency_ms": 0,
            "cpu_workers": 0, "leaked_chunks": 0,
        })

    # Reset counters
    with _stats_lock:
        _total_requests = 0
        _error_requests = 0
    error_rate_gauge.set(0)

    for f in ("error_mode", "cpu_spike", "memory_leak", "high_latency", "cascade"):
        fault_active_gauge.labels(fault=f).set(0)

    app.logger.info("FAULT_RESET all faults cleared")
    return jsonify({"status": "reset", "faults": "cleared"})


# ── Prometheus scrape ──────────────────────────────────────────────────────────

@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.logger.info(f"SERVER_STARTED port={port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
