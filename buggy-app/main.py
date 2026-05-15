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
    "code_bug":      False,   # ZeroDivisionError in _get_avg_response_ms (empty window)
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

# Pre-populated so the app starts healthy by default.
# /fault/code_bug clears this window to trigger the bug; pod restart also
# starts with an empty window when CODE_BUG_ACTIVE=1 is set in the Deployment.
_STARTUP_SAFE_SAMPLES = [20.0, 25.0, 30.0, 22.0, 28.0, 35.0, 18.0, 24.0, 32.0, 26.0]
_latency_samples: list[float] = list(_STARTUP_SAFE_SAMPLES)

# Honour env var so a Deployment with CODE_BUG_ACTIVE=1 starts broken immediately
# (persists the bug across pod restarts without needing the HTTP fault endpoint).
if os.environ.get("CODE_BUG_ACTIVE", "0") == "1":
    _latency_samples.clear()
    _state["code_bug"] = True


def _get_avg_response_ms() -> float:
    """Return average of recent response times in ms.

    Programmatic bug: no empty-list guard.
    Raises ZeroDivisionError whenever _latency_samples is empty — e.g. right
    after pod start (CODE_BUG_ACTIVE=1) or after /fault/code_bug clears the window.
    Fix: add  `if not _latency_samples: return 0.0`  before the return.
    """
    return sum(_latency_samples) / len(_latency_samples)


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
    """Liveness probe — always 200 as long as the process is alive.
    Must NOT check fault state; faults are intentional and should not trigger pod restarts.
    """
    return jsonify({"status": "alive", "uptime": f"{time.time() - _start_time:.0f}s"})


@app.route("/healthz")
def healthz():
    """Readiness / general health — reflects fault state but never used by liveness probe."""
    with _lock:
        faults = {k: v for k, v in _state.items() if isinstance(v, bool) and v}
    if faults:
        return jsonify({"status": "degraded", "active_faults": list(faults.keys()),
                        "uptime": f"{time.time() - _start_time:.0f}s"})
    return jsonify({
        "status": "healthy",
        "uptime": f"{time.time() - _start_time:.0f}s",
    })


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

    if hi_latency or cascade:
        time.sleep((latency + random.randint(0, 100)) / 1000)
    else:
        time.sleep(random.uniform(0.010, 0.050))

    err_prob = 0.9 if cascade else err_rate
    if err_prob > 0 and random.random() < err_prob:
        app.logger.error("request failed route=/api/data reason=fault_injection status=500")
        return jsonify({"error": "simulated server fault"}), 500

    # 4xx fault: simulates auth/config breakage (e.g. bad deploy rotated API key)
    if client_rate > 0 and random.random() < client_rate:
        app.logger.warning("request rejected route=/api/data reason=client_error_fault status=403")
        return jsonify({"error": "forbidden — auth config broken (simulated)"}), 403

    # Stats bug: _compute_percentile uses wrong index multiplier → IndexError on every request.
    if stats_bug:
        samples = _latency_samples if _latency_samples else [1.0]
        p99 = _compute_percentile(samples, 0.99)   # IndexError: int(99 * N) >> N
        return jsonify({
            "data": "ok",
            "p99_ms": round(p99, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Always compute average response time.
    # _get_avg_response_ms() has no guard for an empty window — programmatic bug.
    # Raises ZeroDivisionError when _latency_samples is empty (after pod start or
    # after /fault/code_bug clears the window).
    avg_ms = _get_avg_response_ms()
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
    """Inject 4xx errors — simulates bad deploy that broke auth/config."""
    data = request.get_json(silent=True) or {}
    rate = float(data.get("rate", 0.8))
    with _lock:
        _state["client_errors"]      = True
        _state["client_error_rate"]  = rate
    fault_active_gauge.labels(fault="client_errors").set(1)
    app.logger.warning(f"FAULT_INJECTED fault=client_errors rate={rate} status=403")
    return jsonify({"fault": "client_errors", "status": "active",
                    "http_status": 403, "rate": rate,
                    "note": "simulates broken auth/config post-deploy"})


@app.route("/fault/code_bug", methods=["POST"])
def fault_code_bug():
    """Trigger the programmatic bug already present in _get_avg_response_ms().

    The function has no empty-list guard (real code defect). Clearing the latency
    window forces it to fire immediately on every subsequent request.
    The same crash occurs naturally on pod restart (window starts empty) when
    the Deployment carries CODE_BUG_ACTIVE=1.
    """
    _latency_samples.clear()
    with _lock:
        _state["code_bug"] = True
    fault_active_gauge.labels(fault="code_bug").set(1)
    app.logger.error(
        "FAULT_INJECTED fault=code_bug — "
        "_get_avg_response_ms will raise ZeroDivisionError (empty latency window)"
    )
    return jsonify({
        "fault": "code_bug",
        "status": "active",
        "effect": "ZeroDivisionError on every /api/data request (empty latency window)",
        "persists_on_restart": "yes — set CODE_BUG_ACTIVE=1 in Deployment env",
    })


@app.route("/fault/stats_bug", methods=["POST"])
def fault_stats_bug():
    """Activate a percentile calculation bug: wrong index multiplier → IndexError.

    Simulates a developer shipping _compute_percentile() without dividing
    percentile by 100, so idx = int(99 * N) immediately overflows the list.
    Populates the latency window with dummy samples so the bug fires on the
    very first request (doesn't require any warm-up traffic).
    """
    with _lock:
        _state["stats_bug"] = True
    # Pre-fill the window so _compute_percentile receives a non-empty list
    # and the IndexError fires immediately on the first /api/data request.
    if not _latency_samples:
        _latency_samples.extend([float(i) for i in range(1, 51)])
    fault_active_gauge.labels(fault="stats_bug").set(1)
    app.logger.error(
        "FAULT_INJECTED fault=stats_bug — "
        "_compute_percentile will raise IndexError (wrong percentile index multiplier)"
    )
    return jsonify({
        "fault": "stats_bug",
        "status": "active",
        "effect": "IndexError on every /api/data request (int(99 * N) out of bounds)",
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
            "error_mode": False, "client_errors": False,
            "cpu_spike": False, "memory_leak": False,
            "high_latency": False, "cascade": False,
            "code_bug": False, "stats_bug": False,
            "error_rate": 0.0, "client_error_rate": 0.0,
            "latency_ms": 0, "cpu_workers": 0, "leaked_chunks": 0,
        })

    # Reset counters
    with _stats_lock:
        _total_requests  = 0
        _error_requests  = 0
        _client_requests = 0
    error_rate_gauge.set(0)
    http_4xx_rate_gauge.set(0)

    # Restore safe latency values so _get_avg_response_ms() stops crashing after reset.
    _latency_samples.clear()
    _latency_samples.extend(_STARTUP_SAFE_SAMPLES)

    for f in ("error_mode", "client_errors", "cpu_spike", "memory_leak", "high_latency", "cascade", "code_bug", "stats_bug"):
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
