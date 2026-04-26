"""Self-Healing Agent — Python implementation.
Full agentic loop: Perceive → Reason → Plan → Act → Verify → Remember

Run:
    python main.py
    DRY_RUN=true python main.py
"""
import logging
import signal
import sys
import threading
import time

from rich.console import Console

import config
from agentmetrics import metrics as agentmetrics
from perception.prometheus import PrometheusClient, ClusterMetrics
from perception.loki import LokiClient
from planning.decision import DecisionEngine, ActionPlan
from reasoning.analyzer import Analyzer
from action.executor import Executor
from memory.store import Store
from utils.slo import check_slos
from utils.verifier import verify_and_record

console = Console()


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="time=%(asctime)sZ level=%(levelname)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


logger = logging.getLogger(__name__)


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    def __init__(self):
        self.prometheus = PrometheusClient()
        self.loki       = LokiClient()
        self.analyzer   = Analyzer()
        self.decision   = DecisionEngine()
        self.executor   = Executor()
        self.store      = Store()
        self.cycle      = 0

    def run(self):
        _print_banner()
        stop = threading.Event()

        def _on_signal(sig, _frame):
            logger.info("shutdown signal received — stopping agent")
            stop.set()

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT,  _on_signal)

        self._run_cycle()
        while not stop.is_set():
            stop.wait(timeout=config.POLL_INTERVAL)
            if not stop.is_set():
                self._run_cycle()

    def _run_cycle(self):
        self.cycle += 1
        agentmetrics.CYCLES.inc()
        _print_divider(f"Agent Cycle #{self.cycle}")

        # ── 1. PERCEIVE ──────────────────────────────────────────────────────
        logger.info("collecting metrics")
        try:
            metrics = self.prometheus.collect_metrics()
        except Exception as e:
            logger.error(f"metrics collection failed: {e}")
            return

        # ── 2. SLO CHECK ─────────────────────────────────────────────────────
        violations = check_slos(metrics)
        _print_metrics_table(metrics)

        if not violations:
            agentmetrics.SLO_CHECKS.labels(result="healthy").inc()
            logger.info("all SLOs healthy — no LLM analysis needed")
            return

        incident_start = time.time()
        agentmetrics.SLO_CHECKS.labels(result="violated").inc()
        _print_violations(violations)

        # ── 3. REASON — tool-use loop ─────────────────────────────────────────
        logger.info("starting LLM investigation")
        try:
            diag = self.analyzer.analyze(violations, metrics, self.loki, self.store)
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            return
        _print_diagnosis(diag)
        agentmetrics.INCIDENTS.labels(severity=diag.severity).inc()

        # ── 4. PLAN ──────────────────────────────────────────────────────────
        plan = self.decision.select_action(diag, metrics)
        _print_plan(plan)

        # ── 5. ACT ───────────────────────────────────────────────────────────
        action_taken  = False
        action_result = "skipped"

        if plan.action != "no_action" and plan.safe:
            result = self.executor.execute(plan)
            self.decision.record_executed(plan.action)
            action_result = result.status
            action_taken  = True
            agentmetrics.ACTIONS_EXECUTED.labels(action=plan.action).inc()

        # ── 6. REMEMBER ──────────────────────────────────────────────────────
        inc = self.store.record(
            anomalies=diag.anomalies,
            root_cause=diag.root_cause,
            severity=diag.severity,
            action=plan.action,
            result=action_result,
            snapshot={
                "error_rate":   metrics.error_rate,
                "latency_p99":  metrics.latency_p99_ms,
                "cpu_usage":    metrics.cpu_usage,
                "memory_usage": metrics.memory_usage,
                "pod_restarts": metrics.pod_restarts,
            },
        )

        # ── 7. VERIFY ────────────────────────────────────────────────────────
        if action_taken and not config.DRY_RUN:
            threading.Thread(
                target=verify_and_record,
                args=(inc.id, plan.action, incident_start, self.prometheus, self.store),
                daemon=True,
            ).start()


# ── Terminal rendering ─────────────────────────────────────────────────────────

def _print_banner():
    model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.CLAUDE_MODEL
    console.print()
    console.print("[cyan]╔══════════════════════════════════════════════════════════╗[/cyan]")
    console.print("[cyan]║    Agentic DevOps Self-Healing System  (Python Edition)  ║[/cyan]")
    console.print(f"[cyan]║[/cyan]  Target:   [cyan]{config.TARGET_NAMESPACE}[/cyan] / [cyan]{config.TARGET_DEPLOYMENT}[/cyan]")
    console.print(f"[cyan]║[/cyan]  LLM:      [yellow]{config.LLM_BACKEND}[/yellow] ([yellow]{model}[/yellow])")
    console.print(f"[cyan]║[/cyan]  Poll:     [yellow]{config.POLL_INTERVAL}[/yellow]s  |  Dry-run: [yellow]{config.DRY_RUN}[/yellow]")
    console.print(f"[cyan]║[/cyan]  Metrics:  [yellow]http://localhost:{config.METRICS_PORT}/metrics[/yellow]")
    console.print("[cyan]╚══════════════════════════════════════════════════════════╝[/cyan]")
    console.print()


def _print_divider(title: str):
    bar = "─" * 20
    console.print(f"\n[cyan]{bar}[/cyan] [bold]{title}[/bold] [cyan]{bar}[/cyan]")


def _print_metrics_table(m: ClusterMetrics):
    console.print()
    console.print(f"  [bold]{'Metric':<24} {'Value':<14} {'SLO':<14} Status[/bold]")
    console.print("  " + "─" * 70)

    def row(name: str, value: str, slo: str, ok: bool):
        status = "[green]OK[/green]" if ok else "[red]BREACH[/red]"
        console.print(f"  {name:<24} {value:<14} {slo:<14} {status}")

    row("Error Rate",        f"{m.error_rate*100:.2f}%",    f"<{config.SLO_ERROR_RATE*100:.0f}%",  m.error_rate <= config.SLO_ERROR_RATE)
    row("P99 Latency (ms)",  f"{m.latency_p99_ms:.0f}ms",  f"<{config.SLO_LATENCY_MS:.0f}ms",     m.latency_p99_ms <= config.SLO_LATENCY_MS)
    row("CPU Usage",         f"{m.cpu_usage*100:.2f}%",     f"<{config.SLO_CPU*100:.0f}%",         m.cpu_usage <= config.SLO_CPU)
    row("Memory Usage",      f"{m.memory_usage*100:.2f}%",  f"<{config.SLO_MEMORY*100:.0f}%",      m.memory_usage <= config.SLO_MEMORY)
    row("Pod Restarts (5m)", str(m.pod_restarts),           "≤3",                                   m.pod_restarts <= 3)
    row("Ready Replicas",    f"{m.ready_replicas}/{m.desired_replicas}", "=desired",                m.ready_replicas == m.desired_replicas)
    console.print()


def _print_violations(violations: list[str]):
    console.print("[red]╭─ SLO Violations " + "─" * 50 + "╮[/red]")
    for v in violations:
        console.print(f"[red]│ • {v}[/red]")
    console.print("[red]╰" + "─" * 68 + "╯[/red]")


def _print_diagnosis(diag):
    sev_colors = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "green"}
    sc = sev_colors.get(diag.severity, "white")
    console.print("[magenta]╭─ LLM Diagnosis " + "─" * 51 + "╮[/magenta]")
    console.print(f"[magenta]│[/magenta] [bold]Root Cause:[/bold] {diag.root_cause}")
    console.print(f"[magenta]│[/magenta] [bold]Severity:[/bold]   [{sc}]{diag.severity.upper()}[/{sc}]")
    console.print(f"[magenta]│[/magenta] [bold]Confidence:[/bold] {diag.confidence*100:.0f}%")
    console.print(f"[magenta]│[/magenta] [bold]Suggested:[/bold]  {', '.join(diag.suggested_actions)}")
    console.print("[magenta]╰" + "─" * 68 + "╯[/magenta]")


def _print_plan(plan: ActionPlan):
    c = "green" if plan.action == "no_action" else "yellow"
    console.print(f"[{c}]╭─ Decision " + "─" * 56 + f"╮[/{c}]")
    console.print(f"[{c}]│[/{c}] [bold]Action:[/bold] [{c}]{plan.action.upper()}[/{c}]")
    console.print(f"[{c}]│[/{c}] [bold]Params:[/bold] {plan.params}")
    console.print(f"[{c}]│[/{c}] [bold]Reason:[/bold] {plan.reason}")
    console.print(f"[{c}]│[/{c}] [bold]Safe:[/bold]   {plan.safe}")
    console.print(f"[{c}]╰[/{c}]" + "─" * 68 + f"[{c}]╯[/{c}]")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _setup_logging()

    if config.LLM_BACKEND == "claude" and not config.ANTHROPIC_KEY:
        logger.error("ANTHROPIC_API_KEY must be set when LLM_BACKEND=claude")
        sys.exit(1)

    agentmetrics.start_server(config.METRICS_PORT)

    try:
        agent = Agent()
    except Exception as e:
        logger.error(f"failed to initialise agent: {e}")
        sys.exit(1)

    agent.run()


if __name__ == "__main__":
    main()
