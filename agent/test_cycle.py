"""Single-cycle integration test — runs one agent cycle and exits.

Usage:
    python test_cycle.py
    DRY_RUN=true HUMAN_IN_LOOP=false python test_cycle.py
"""
import os, sys
os.environ.setdefault("DRY_RUN",         "true")
os.environ.setdefault("HUMAN_IN_LOOP",   "false")

import logging_setup
logging_setup.setup()

from agents.orchestrator import OrchestratorAgent
from perception.prometheus import PrometheusClient
from perception.loki import LokiClient
from planning.decision import DecisionEngine
from action.executor import Executor
from memory.store import Store
from utils.slo import check_slos
import config, time
from rich.console import Console

console = Console()

def run():
    console.print("\n[bold cyan]══ Single-Cycle Test ══[/bold cyan]\n")

    prom        = PrometheusClient()
    loki        = LokiClient()
    store       = Store()
    orchestrator = OrchestratorAgent()
    decision    = DecisionEngine()
    executor    = Executor()

    # 1. PERCEIVE
    console.print("[bold]① Perceiving metrics…[/bold]")
    metrics = prom.collect_metrics()
    console.print(f"   5xx={metrics.error_rate*100:.2f}%  4xx={metrics.http_4xx_rate*100:.2f}%  "
                  f"P99={metrics.latency_p99_ms:.0f}ms  CPU={metrics.cpu_usage*100:.1f}%")

    # 2. SLO CHECK
    violations = check_slos(metrics)
    if not violations:
        console.print("\n[green]All SLOs healthy — nothing to investigate.[/green]")
        return
    console.print(f"\n[bold red]② SLO violations ({len(violations)}):[/bold red]")
    for v in violations:
        console.print(f"   • {v}")

    # 3. REASON
    incident_id = f"test-{time.time_ns()}"
    console.print(f"\n[bold magenta]③ Agents investigating…  (incident={incident_id})[/bold magenta]")
    t0   = time.monotonic()
    diag = orchestrator.investigate(violations, metrics, loki, store, incident_id)
    elapsed = int((time.monotonic() - t0) * 1000)

    console.print(f"\n[magenta]   Root cause : {diag.root_cause}[/magenta]")
    console.print(f"[magenta]   Severity   : {diag.severity.upper()}[/magenta]")
    console.print(f"[magenta]   Confidence : {diag.confidence*100:.0f}%[/magenta]")
    console.print(f"[magenta]   Actions    : {diag.suggested_actions}[/magenta]")
    console.print(f"[magenta]   Reasoning  : {diag.reasoning}[/magenta]")
    console.print(f"[magenta]   Duration   : {elapsed}ms[/magenta]")

    # 4. PLAN
    plan = decision.select_action(diag, metrics)
    color = "green" if plan.action == "no_action" else "yellow"
    console.print(f"\n[bold {color}]④ Decision: {plan.action.upper()}[/bold {color}]")
    console.print(f"   Reason : {plan.reason}")
    console.print(f"   Safe   : {plan.safe}  |  DRY_RUN={config.DRY_RUN}")

    # 5. ACT
    if plan.action != "no_action" and plan.safe:
        result = executor.execute(plan)
        console.print(f"\n[bold]⑤ Executor: {result.status} — {result.detail}[/bold]")
    else:
        console.print(f"\n[dim]⑤ No action taken[/dim]")

    console.print("\n[bold cyan]══ Test cycle complete ══[/bold cyan]\n")

if __name__ == "__main__":
    run()
