#!/usr/bin/env python3
"""Display a Rich scenario title panel. Usage: python show_title.py <1|2|3|4|intro|outro>"""
import sys
from rich.console import Console
from rich.panel import Panel

c = Console()
n = sys.argv[1] if len(sys.argv) > 1 else "intro"

PANELS = {
    "intro": Panel(
        "[bold green]Agentic Self-Healing Kubernetes[/bold green]\n\n"
        "  Four SLO breach scenarios — real cluster, real LLM, no mocks\n\n"
        "  [dim]Perceive → Reason (multi-agent) → Plan → Act → Verify → Remember[/dim]",
        border_style="green", expand=False,
    ),
    "1": Panel(
        "[bold red]SCENARIO 1[/bold red]  —  High Error Rate  →  [bold]restart_pods[/bold]\n\n"
        "  [dim]5xx rate spikes above 1% SLO[/dim]\n"
        "  [dim]Agent detects breach, dispatches MetricsAgent + LogsAgent[/dim]\n"
        "  [dim]Orchestrator selects restart_pods — pods replaced, SLOs recover[/dim]",
        border_style="red", expand=False,
    ),
    "2": Panel(
        "[bold yellow]SCENARIO 2[/bold yellow]  —  P99 Latency Spike  →  [bold]scale_up[/bold]\n\n"
        "  [dim]600ms delay added — P99 latency breaches 200ms SLO[/dim]\n"
        "  [dim]Agent detects latency root cause, scales deployment out[/dim]\n"
        "  [dim]Verifier polls until all replicas ready — SLOs recover[/dim]",
        border_style="yellow", expand=False,
    ),
    "3": Panel(
        "[bold magenta]SCENARIO 3[/bold magenta]  —  Code Bug  →  [bold]patch_code[/bold]  +  GitHub PR\n\n"
        "  [dim]TypeError in _format_response_metadata — every request returns 500[/dim]\n"
        "  [dim]LogsAgent reads stack trace, orchestrator selects patch_code[/dim]\n"
        "  [dim]CodePatchAgent: list files → read source → propose fix → git commit → PR[/dim]",
        border_style="magenta", expand=False,
    ),
    "4": Panel(
        "[bold cyan]SCENARIO 4[/bold cyan]  —  Stats Bug  →  [bold]patch_code[/bold]  +  GitHub PR\n\n"
        "  [dim]IndexError in _compute_percentile — int(99 * N) overflows list bounds[/dim]\n"
        "  [dim]LogsAgent reads IndexError trace, orchestrator selects patch_code[/dim]\n"
        "  [dim]CodePatchAgent: reads source → fixes index calc → git commit → PR[/dim]",
        border_style="cyan", expand=False,
    ),
    "outro": Panel(
        "[bold green]Four SLO breaches — detected and fixed autonomously[/bold green]\n\n"
        "  [cyan]Multi-agent investigation[/cyan]  — evidence gate enforces ≥2 specialists\n"
        "  [cyan]Structural confidence[/cyan]      — specialist avg + historical accuracy\n"
        "  [cyan]SLO hysteresis[/cyan]             — sustained window before acting\n"
        "  [cyan]Verification polling[/cyan]        — waits for pods ready before checking SLOs\n"
        "  [cyan]Code patch pipeline[/cyan]         — CodePatchAgent → git → GitHub PR",
        border_style="green", expand=False,
    ),
}

panel = PANELS.get(n, PANELS["intro"])
c.print()
c.print(panel)
c.print()
