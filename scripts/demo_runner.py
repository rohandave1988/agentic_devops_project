#!/usr/bin/env python3
"""
Demo runner — orchestrates a real end-to-end demo:
  1. Shows cluster health
  2. Injects CPU fault BEFORE starting agent (gives rate() window time to fill)
  3. Starts the agent (inherits terminal — Rich colors appear naturally)
  4. Lets the agent detect breach, run LLM tool-use loop, execute fix, verify
  5. Displays agent metrics summary + Grafana dashboard URL

No mocks. No hardcoded output. Everything is real.

Usage:
    export LLM_BACKEND=claude          # or ollama
    export ANTHROPIC_API_KEY=sk-ant-…  # if using claude
    python3 scripts/demo_runner.py
"""
import os
import sys
import subprocess
import time
import urllib.request
import urllib.error

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.rule import Rule
    from rich import box
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "rich", "-q"], check=False)
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.rule import Rule
    from rich import box

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR   = os.path.join(REPO_ROOT, "agent")
APP_URL     = "http://localhost:30080"
AGENT_METRICS_URL = "http://localhost:8080/metrics"
GRAFANA_URL = "http://localhost:30300"

# Agent behaviour overrides for demo (shorter cycles).
# Prometheus is exposed via NodePort 30090; Loki is port-forwarded to 3100.
SKIP_SETUP = os.environ.get("SKIP_SETUP", "").lower() in ("1", "true", "yes")

DEMO_ENV_OVERRIDES = {
    "VERIFY_DELAY_SEC":        "30",
    "AGENT_POLL_INTERVAL_SEC": "5",
    "COOLDOWN_PERIOD_SEC":     "60",
    "PROMETHEUS_URL":          "http://localhost:30090",
    "LOKI_URL":                "http://localhost:3100",
    "PROMETHEUS_RATE_WINDOW":  "30s",
}

console = Console()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, **kwargs)


_loki_pf: subprocess.Popen | None = None


def _start_loki_portforward() -> subprocess.Popen | None:
    """Port-forward Loki to localhost:3100 if not already reachable."""
    try:
        with urllib.request.urlopen("http://localhost:3100/ready", timeout=2):
            return None  # already reachable
    except Exception:
        pass
    console.print("[dim]Starting Loki port-forward (ClusterIP → localhost:3100)...[/dim]")
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "svc/loki", "3100:3100", "-n", "monitoring"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)  # give port-forward time to bind
    return proc


def _inject_fault():
    console.print("[bold red]► Injecting CPU fault into buggy-app...[/bold red]")
    # Reset any existing fault first for a clean state
    try:
        _run(["curl", "-s", "-X", "POST", f"{APP_URL}/fault/reset"])
    except Exception:
        pass
    time.sleep(1)
    try:
        r = _run(["curl", "-s", "-X", "POST", f"{APP_URL}/fault/cpu"])
        if r.stdout:
            console.print(f"  [dim]{r.stdout.strip()}[/dim]")
    except Exception as e:
        console.print(f"  [yellow]Warning: fault injection failed — {e}[/yellow]")
    console.print("  [dim]CPU burn workers started (rate() window filling...)[/dim]")


def _reset_fault():
    try:
        _run(["curl", "-s", "-X", "POST", f"{APP_URL}/fault/reset"])
    except Exception:
        pass


def _fetch_metrics() -> dict:
    """Scrape the agent's Prometheus endpoint. Returns {} on connection failure."""
    metrics: dict = {}
    try:
        with urllib.request.urlopen(AGENT_METRICS_URL, timeout=5) as resp:
            for line in resp.read().decode().splitlines():
                if line.startswith("#"):
                    continue
                parts = line.rsplit(" ", 1)
                if len(parts) != 2:
                    continue
                name_labels, val = parts[0], parts[1]
                try:
                    fval = float(val)
                except ValueError:
                    continue

                if name_labels == "agent_cycles_total":
                    metrics["cycles"] = int(fval)
                elif name_labels.startswith("agent_incidents_total"):
                    metrics["incidents"] = metrics.get("incidents", 0) + int(fval)
                elif name_labels.startswith("agent_actions_executed_total"):
                    metrics["actions"] = metrics.get("actions", 0) + int(fval)
                elif name_labels.startswith("agent_verifications_total") and "resolved" in name_labels:
                    metrics["verifications_ok"] = metrics.get("verifications_ok", 0) + int(fval)
                elif name_labels == "agent_mttr_seconds_sum":
                    metrics["mttr_sum"] = fval
                elif name_labels == "agent_mttr_seconds_count":
                    metrics["mttr_count"] = fval
    except (urllib.error.URLError, OSError):
        return {}   # connection failed — return nothing so caller keeps last good snapshot

    s = metrics.get("mttr_sum", 0)
    c = metrics.get("mttr_count", 0)
    metrics["mttr"] = f"{s / c:.0f}s" if c > 0 else "—"
    return metrics


def _show_summary(m: dict | None = None):
    m = m or _fetch_metrics()
    console.print()
    console.print(Rule("[bold cyan]Agent Run Summary[/bold cyan]"))

    t = Table(box=box.ROUNDED, border_style="cyan", show_header=True)
    t.add_column("Metric", style="bold white", min_width=28)
    t.add_column("Value", justify="right", style="bold green", min_width=10)

    t.add_row("Cycles executed",        str(m.get("cycles", "—")))
    t.add_row("Incidents detected",     str(m.get("incidents", "—")))
    t.add_row("Remediation actions",    str(m.get("actions", "—")))
    t.add_row("Verifications passed",   str(m.get("verifications_ok", "—")))
    t.add_row("Mean Time To Recovery",  m.get("mttr", "—"))
    console.print(t)

    console.print()
    console.print(Panel(
        f"[bold cyan]{GRAFANA_URL}[/bold cyan]\n\n"
        "[dim]SLO Dashboard   →  Browse > Dashboards > SLO Overview[/dim]\n"
        "[dim]Agent Dashboard →  Browse > Dashboards > Agent Metrics[/dim]\n\n"
        "[dim]Credentials: admin / admin123[/dim]",
        title="[bold green]Grafana Live Dashboards[/bold green]",
        border_style="green",
        expand=False,
    ))
    console.print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    console.print(Panel(
        "[bold green]Agentic DevOps — Live Demo[/bold green]\n"
        "[dim]Self-healing Kubernetes · Real cluster · No mocks[/dim]",
        border_style="green",
        expand=False,
    ))
    console.print()

    if SKIP_SETUP:
        # pre_demo.sh already handled fault injection + rate() warmup
        console.print("[bold]Current cluster state:[/bold]")
        r = _run(["kubectl", "get", "pods", "-n", "demo"])
        console.print(r.stdout.strip())
        console.print()
        console.print("[dim]Fault pre-injected · rate(30s) window ready · starting agent...[/dim]")
        console.print()
        loki_pf = None
    else:
        # ── 1. Reset cluster to clean demo baseline ────────────────────────────
        console.print("[dim]Resetting buggy-app to 1 replica for demo...[/dim]")
        _run(["kubectl", "scale", "deployment/buggy-app", "--replicas=1", "-n", "demo"])
        _run(["kubectl", "rollout", "status", "deployment/buggy-app", "-n", "demo", "--timeout=60s"])

        console.print("[bold]Current cluster state:[/bold]")
        r = _run(["kubectl", "get", "pods", "-n", "demo"])
        console.print(r.stdout.strip())
        console.print()

        # ── 1b. Ensure Loki is reachable ──────────────────────────────────────
        loki_pf = _start_loki_portforward()

        # ── 2. Inject fault BEFORE starting agent ─────────────────────────────
        _inject_fault()
        console.print()

        console.print("[dim]Waiting 35s before starting agent (rate(30s) window warming)...[/dim]")
        time.sleep(35)
        console.print()

    # ── 3. Build agent env ─────────────────────────────────────────────────────
    agent_env = {**os.environ}
    agent_env.update(DEMO_ENV_OVERRIDES)

    # Ensure .env exists so dotenv doesn't complain
    env_path = os.path.join(AGENT_DIR, ".env")
    if not os.path.exists(env_path):
        example = os.path.join(AGENT_DIR, ".env.example")
        if os.path.exists(example):
            import shutil
            shutil.copy(example, env_path)

    # ── 4. Start agent (inherits terminal — Rich renders in colour) ────────────
    console.print(Rule("[bold]Agent starting[/bold]"))
    console.print()

    agent_proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=AGENT_DIR,
        env=agent_env,
        # No stdout/stderr redirect — agent writes directly to this terminal
    )

    # Scrape metrics in the background every 5s during the entire agent run.
    # The last snapshot before the process exits is used for the summary.
    import threading as _threading
    _latest_metrics: dict = {}
    _scrape_stop = _threading.Event()

    def _scrape_loop():
        while not _scrape_stop.is_set():
            snap = _fetch_metrics()
            if snap:
                _latest_metrics.update(snap)
            _scrape_stop.wait(5)

    scraper = _threading.Thread(target=_scrape_loop, daemon=True)
    scraper.start()

    try:
        # Agent runs until timeout:
        #   t+0:   cycles begin (poll every 5s, rate window=30s)
        #   t+10:  breach detected, LLM investigates (~80s tool loop)
        #   t+90:  scale_up dispatched, verifier waits 30s
        #   t+120: verifier confirms SLO recovery
        # SKIP_SETUP mode uses a shorter window — breach hits in cycle 1.
        _timeout = 140 if SKIP_SETUP else 170
        agent_proc.wait(timeout=_timeout)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — stopping agent.[/yellow]")

    # ── 5. Stop agent; use the last scraped metrics snapshot ──────────────────
    agent_proc.terminate()
    agent_proc.wait()
    _scrape_stop.set()
    summary_metrics = _latest_metrics

    _reset_fault()
    if loki_pf:
        loki_pf.terminate()
    _show_summary(summary_metrics)
    time.sleep(20)  # keep terminal alive so VHS captures summary before process exit


if __name__ == "__main__":
    main()
