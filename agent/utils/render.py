"""Terminal rendering helpers — rich-formatted output for the agent loop."""
from rich.console import Console

import config
from perception.prometheus import ClusterMetrics
from planning.decision import ActionPlan

console = Console()


def print_banner():
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


def print_divider(title: str):
    bar = "─" * 20
    console.print(f"\n[cyan]{bar}[/cyan] [bold]{title}[/bold] [cyan]{bar}[/cyan]")


def print_metrics_table(m: ClusterMetrics):
    console.print()
    console.print(f"  [bold]{'Metric':<24} {'Value':<14} {'SLO':<14} Status[/bold]")
    console.print("  " + "─" * 70)

    def row(name: str, value: str, slo: str, ok: bool):
        status = "[green]OK[/green]" if ok else "[red]BREACH[/red]"
        console.print(f"  {name:<24} {value:<14} {slo:<14} {status}")

    row("5xx Error Rate",     f"{m.error_rate*100:.2f}%",          f"<{config.SLO_ERROR_RATE*100:.0f}%",     m.error_rate <= config.SLO_ERROR_RATE)
    row("4xx Error Rate",     f"{m.http_4xx_rate*100:.2f}%",       f"<{config.SLO_4XX_RATE*100:.0f}%",       m.http_4xx_rate <= config.SLO_4XX_RATE)
    row("P99 Latency (ms)",   f"{m.latency_p99_ms:.0f}ms",        f"<{config.SLO_LATENCY_MS:.0f}ms",        m.latency_p99_ms <= config.SLO_LATENCY_MS)
    row("P50 Latency (ms)",   f"{m.latency_p50_ms:.0f}ms",        f"<{config.SLO_LATENCY_P50:.0f}ms",       m.latency_p50_ms <= config.SLO_LATENCY_P50)
    row("CPU Usage",          f"{m.cpu_usage*100:.2f}%",           f"<{config.SLO_CPU*100:.0f}%",            m.cpu_usage <= config.SLO_CPU)
    row("CPU Throttle",       f"{m.cpu_throttle_ratio*100:.1f}%",  f"<{config.SLO_CPU_THROTTLE*100:.0f}%",  m.cpu_throttle_ratio <= config.SLO_CPU_THROTTLE)
    row("Memory Usage",       f"{m.memory_usage*100:.2f}%",        f"<{config.SLO_MEMORY*100:.0f}%",         m.memory_usage <= config.SLO_MEMORY)
    row("OOM Kills (5m)",     str(m.oom_kills),                    "=0",                                      m.oom_kills == 0)
    row("Active Requests",    str(m.active_requests),              f"≤{config.SLO_ACTIVE_REQUESTS}",          m.active_requests <= config.SLO_ACTIVE_REQUESTS)
    row("Pod Restarts (5m)",  str(m.pod_restarts),                 "≤3",                                      m.pod_restarts <= 3)
    row("Ready Replicas",     f"{m.ready_replicas}/{m.desired_replicas}", "=desired",                         m.ready_replicas == m.desired_replicas)
    console.print()


def print_violations(violations: list[str]):
    console.print("[red]╭─ SLO Violations " + "─" * 50 + "╮[/red]")
    for v in violations:
        console.print(f"[red]│ • {v}[/red]")
    console.print("[red]╰" + "─" * 68 + "╯[/red]")


def print_diagnosis(diag):
    sev_colors = {"critical": "red", "high": "yellow", "medium": "cyan", "low": "green"}
    sc = sev_colors.get(diag.severity, "white")
    console.print("[magenta]╭─ LLM Diagnosis " + "─" * 51 + "╮[/magenta]")
    console.print(f"[magenta]│[/magenta] [bold]Root Cause:[/bold] {diag.root_cause}")
    console.print(f"[magenta]│[/magenta] [bold]Severity:[/bold]   [{sc}]{diag.severity.upper()}[/{sc}]")
    console.print(f"[magenta]│[/magenta] [bold]Confidence:[/bold] {diag.confidence*100:.0f}%")
    console.print(f"[magenta]│[/magenta] [bold]Suggested:[/bold]  {', '.join(diag.suggested_actions)}")
    console.print("[magenta]╰" + "─" * 68 + "╯[/magenta]")


def print_plan(plan: ActionPlan):
    c = "green" if plan.action == "no_action" else "yellow"
    console.print(f"[{c}]╭─ Decision " + "─" * 56 + f"╮[/{c}]")
    console.print(f"[{c}]│[/{c}] [bold]Action:[/bold] [{c}]{plan.action.upper()}[/{c}]")
    console.print(f"[{c}]│[/{c}] [bold]Params:[/bold] {plan.params}")
    console.print(f"[{c}]│[/{c}] [bold]Reason:[/bold] {plan.reason}")
    console.print(f"[{c}]│[/{c}] [bold]Safe:[/bold]   {plan.safe}")
    console.print(f"[{c}]╰[/{c}]" + "─" * 68 + f"[{c}]╯[/{c}]")
