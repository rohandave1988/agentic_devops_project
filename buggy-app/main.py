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
error_rate_gauge    = Gauge("app_error_rate",       "Current application 5xx error rate (0–1)")
http_4xx_rate_gauge = Gauge("app_4xx_rate",         "Current application 4xx client error rate (0–1)")
active_requests     = Gauge("app_active_requests",  "Active in-flight requests")
fault_active_gauge  = Gauge("app_fault_active",     "Whether a fault is active (1=yes)", ["fault"])
cpu_usage_gauge     = Gauge("app_cpu_usage_percent",    "Process-group CPU usage percent (0–100)")
memory_usage_gauge  = Gauge("app_memory_usage_percent", "Process-group memory usage percent (0–100)")

# ── Fault state ────────────────────────────────────────────────────────────────

_lock          = threading.Lock()
_state: dict   = {
    "error_mode":    False,
    "client_errors": False,   # 4xx fault
    "cpu_spike":     False,
    "memory_leak":   False,
    "high_latency":  False,
    "cascade":       False,
    "type_bug":      False,   # TypeError in _format_response_metadata (float + str)
    "stats_bug":     False,   # IndexError in _compute_percentile (wrong index multiplier)
    "error_rate":    0.0,
    "client_error_rate": 0.0,
    "latency_ms":    0,
    "cpu_workers":   0,
    "leaked_chunks": 0,
}
_cpu_processes: list[subprocess.Popen] = []
_leaked_blocks:   list[bytes]           = []
_leak_stop:       threading.Event | None = None
_start_time = time.time()

# Rolling latency window — populated by _after() hook
_MAX_SAMPLES = 100
_STARTUP_SAFE_SAMPLES = [20.0, 25.0, 30.0, 22.0, 28.0, 35.0, 18.0, 24.0, 32.0, 26.0]
_latency_samples: list[float] = list(_STARTUP_SAFE_SAMPLES)

# Honour env var so a Deployment with TYPE_BUG_ACTIVE=1 starts broken immediately
# (persists the bug across pod restarts without needing the HTTP fault endpoint).
if os.environ.get("TYPE_BUG_ACTIVE", "0") == "1":
    _state["type_bug"] = True

# Env-driven fault activation — simulates a bad deploy that baked in broken config.
# Set FAULT_CLIENT_ERRORS=1 in the Deployment spec to activate on pod start;
# rollback removes the env var, new pods start clean.
if os.environ.get("FAULT_CLIENT_ERRORS", "0") == "1":
    _rate = float(os.environ.get("FAULT_CLIENT_ERRORS_RATE", "0.85"))
    _state["client_errors"]     = True
    _state["client_error_rate"] = _rate

# Request counters for error rate calculation
_total_requests  = 0
_error_requests  = 0   # 5xx
_client_requests = 0   # 4xx
_stats_lock = threading.Lock()


def _format_response_metadata(elapsed_ms: float, req_count: int) -> dict:
    """Build extra fields appended to every /api/data response.

    BUG: concatenates a float with str using +  →  TypeError on every request.
    Fix: change  elapsed_ms  to  f"{elapsed_ms:.1f}"  (or wrap with str()).
    """
    return {
        "served_by": "buggy-app",
        "timing":    "completed in " + elapsed_ms + "ms",
        "request_n": req_count,
    }


def _compute_percentile(samples: list, percentile: float) -> float:
    """Return the p{percentile} value from a sample list.

    BUG: `percentile` is expected in the 0–100 range (e.g. 99 for p99),
    but the index is computed by multiplying directly without dividing by 100.
    For percentile=99 and len=100, idx = int(99 * 100) = 9900, which is
    far beyond the list bounds → IndexError on every call.
    Fix: change to int((percentile / 100.0) * len(sorted_s)).
    """
    if not samples:
        return 0.0
    sorted_s = sorted(samples)
    idx = int(percentile * len(sorted_s))   # BUG: should be (percentile / 100.0) * len
    return sorted_s[idx]                    # IndexError: list index out of range


def _update_resource_metrics():
    import psutil, os, signal
    proc = psutil.Process(os.getpid())
    while True:
        try:
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

    global _total_requests, _error_requests, _client_requests
    with _stats_lock:
        _total_requests += 1
        if response.status_code >= 500:
            _error_requests += 1
        elif 400 <= response.status_code < 500:
            _client_requests += 1
        rate_5xx = _error_requests  / _total_requests if _total_requests else 0.0
        rate_4xx = _client_requests / _total_requests if _total_requests else 0.0
        if response.status_code < 400 and route == "/api/data":
            _latency_samples.append(duration_ms)
            if len(_latency_samples) > _MAX_SAMPLES:
                _latency_samples.pop(0)
    error_rate_gauge.set(rate_5xx)
    http_4xx_rate_gauge.set(rate_4xx)
    return response


@app.errorhandler(Exception)
def _handle_exception(e):
    """Catch unhandled exceptions so after_request fires and Prometheus counts the 500."""
    app.logger.error(f"UNHANDLED_EXCEPTION route={request.path} error={type(e).__name__}: {e}")
    return jsonify({"error": "internal server error", "type": type(e).__name__}), 500


# ── Health ─────────────────────────────────────────────────────────────────────

@app.route("/livez")
def livez():
    return jsonify({"status": "alive", "uptime": f"{time.time() - _start_time:.0f}s"})


@app.route("/healthz")
def healthz():
    with _lock:
        faults = {k: v for k, v in _state.items() if isinstance(v, bool) and v}
    if faults:
        return jsonify({"status": "degraded", "active_faults": list(faults.keys()),
                        "uptime": f"{time.time() - _start_time:.0f}s"})
    return jsonify({"status": "healthy", "uptime": f"{time.time() - _start_time:.0f}s"})


# ── Business endpoint ──────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    with _lock:
        latency     = _state["latency_ms"]
        err_rate    = _state["error_rate"]
        client_rate = _state["client_error_rate"]
        cascade     = _state["cascade"]
        hi_latency  = _state["high_latency"]
        stats_bug   = _state["stats_bug"]
        type_bug    = _state["type_bug"]

    if hi_latency or cascade:
        time.sleep((latency + random.randint(0, 100)) / 1000)
    else:
        time.sleep(random.uniform(0.010, 0.050))

    err_prob = 0.9 if cascade else err_rate
    if err_prob > 0 and random.random() < err_prob:
        app.logger.error("request failed route=/api/data reason=fault_injection status=500")
        return jsonify({"error": "simulated server fault"}), 500

    if client_rate > 0 and random.random() < client_rate:
        app.logger.warning("request rejected route=/api/data reason=client_error_fault status=403")
        return jsonify({"error": "forbidden — auth config broken (simulated)"}), 403

    # Stats bug: _compute_percentile uses wrong index multiplier → IndexError on every request.
    if stats_bug:
        samples = _latency_samples if _latency_samples else [1.0]
        p99 = _compute_percentile(samples, 99)   # IndexError: int(99 * N) >> N
        return jsonify({
            "data": "ok",
            "p99_ms": round(p99, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Type bug: _format_response_metadata concatenates float with str → TypeError.
    if type_bug:
        elapsed = (time.time() - request._start) * 1000
        with _stats_lock:
            req_n = _total_requests
        elapsed_str = str(round(elapsed, 2))
        req_n_str = str(req_n)
        meta = _format_response_metadata(elapsed_str, req_n_str)   # Fixed TypeError
        return jsonify({"data": "ok", **meta, "timestamp": datetime.now(timezone.utc).isoformat()})

    avg_ms = sum(_latency_samples) / len(_latency_samples) if _latency_samples else 0.0
    return jsonify({
        "data": "ok",
        "avg_response_ms": round(avg_ms, 2),
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

@app.route("/fault/client_errors", methods=["POST"])
def fault_client_errors():
    data = request.get_json(silent=True) or {}
    rate = float(data.get("rate", 0.8))
    with _lock:
        _state["client_errors"]      = True
        _state["client_error_rate"]  = rate
    fault_active_gauge.labels(fault="client_errors").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=client_errors rate={rate} status=403")
    return jsonify({"fault": "client_errors", "status": "active", "http_status": 403, "rate": rate})


@app.route("/fault/type_bug", methods=["POST"])
def fault_type_bug():
    """Activate the TypeError bug in _format_response_metadata.

    Simulates a developer using string concatenation (+) with a float instead of
    an f-string. Raises TypeError: can only concatenate str (not "float") to str
    on every /api/data request.
    Fix: change  elapsed_ms  to  f"{elapsed_ms:.1f}"  inside _format_response_metadata.
    """
    with _lock:
        _state["type_bug"] = True
    fault_active_gauge.labels(fault="type_bug").set(1)
    app.logger.error(
        "FAULT_INJECTED fault=type_bug — "
        "_format_response_metadata will raise TypeError (float + str concatenation)"
    )
    return jsonify({
        "fault":      "type_bug",
        "status":     "active",
        "effect":     "TypeError on every /api/data request (float + str concatenation)",
        "root_cause": "_format_response_metadata uses + operator instead of f-string for elapsed_ms",
        "persists_on_restart": "yes — set TYPE_BUG_ACTIVE=1 in Deployment env",
    })


@app.route("/fault/stats_bug", methods=["POST"])
def fault_stats_bug():
    """Activate a percentile calculation bug: wrong index multiplier → IndexError."""
    with _lock:
        _state["stats_bug"] = True
    if not _latency_samples:
        _latency_samples.extend([float(i) for i in range(1, 51)])
    fault_active_gauge.labels(fault="stats_bug").set(1)
    app.logger.error(
        "FAULT_INJECTED fault=stats_bug — "
        "_compute_percentile will raise IndexError (wrong percentile index multiplier)"
    )
    return jsonify({
        "fault":      "stats_bug",
        "status":     "active",
        "effect":     "IndexError on every /api/data request (int(99 * N) out of bounds)",
        "root_cause": "_compute_percentile multiplies by percentile instead of dividing by 100",
    })


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

    for p in _cpu_processes:
        p.terminate()
    _cpu_processes.clear()

    if _leak_stop:
        _leak_stop.set()
        _leak_stop = None
    _leaked_blocks.clear()

    with _lock:
        _state.update({
            "error_mode": False, "client_errors": False,
            "cpu_spike": False, "memory_leak": False,
            "high_latency": False, "cascade": False,
            "type_bug": False, "stats_bug": False,
            "error_rate": 0.0, "client_error_rate": 0.0,
            "latency_ms": 0, "cpu_workers": 0, "leaked_chunks": 0,
        })

    with _stats_lock:
        _total_requests  = 0
        _error_requests  = 0
        _client_requests = 0
    error_rate_gauge.set(0)
    http_4xx_rate_gauge.set(0)

    _latency_samples.clear()
    _latency_samples.extend(_STARTUP_SAFE_SAMPLES)

    for f in ("error_mode", "client_errors", "cpu_spike", "memory_leak",
              "high_latency", "cascade", "type_bug", "stats_bug"):
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
