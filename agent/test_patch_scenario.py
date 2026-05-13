#!/usr/bin/env python3
"""Quick smoke test for the patch_code pipeline.

Feeds realistic ZeroDivisionError incident data directly to OrchestratorAgent,
then runs CodePatchAgent if the diagnosis is patch_code.

Usage (from agent/ directory):
    python test_patch_scenario.py
    OLLAMA_MODEL=qwen2.5:7b python test_patch_scenario.py
"""
import os, sys, time

# ── Minimal env setup ─────────────────────────────────────────────────────────
os.environ.setdefault("LLM_BACKEND",     "ollama")
os.environ.setdefault("OLLAMA_MODEL",    "qwen2.5:7b")
os.environ.setdefault("ALLOW_CODE_PATCHES", "true")
os.environ.setdefault("HUMAN_IN_LOOP",   "false")
os.environ.setdefault("LOG_LEVEL",       "DEBUG")  # show tool calls
os.environ.setdefault("LANGFUSE_ENABLED","false")
os.environ.setdefault("OTLP_ENDPOINT",   "")

sys.path.insert(0, os.path.dirname(__file__))

from dataclasses import dataclass, field

# ── Fake metrics snapshot ──────────────────────────────────────────────────────
@dataclass
class FakeMetrics:
    error_rate:       float = 0.95   # 95% 5xx
    http_4xx_rate:    float = 0.00
    latency_p99_ms:   float = 180.0
    cpu_usage:        float = 0.22
    memory_usage:     float = 0.31
    pod_restarts:     int   = 0
    oom_kills:        int   = 0
    ready_replicas:   int   = 1
    desired_replicas: int   = 1

# ── Fake Loki stub ─────────────────────────────────────────────────────────────
class FakeLoki:
    def query_recent_logs(self, lookback_sec=120):
        return [
            'ERROR [buggy-app] ZeroDivisionError: division by zero',
            '  File "main.py", line 87, in _handle_data',
            '    p99 = sum(samples) / len(samples) / 0',
            'ZeroDivisionError: division by zero',
            'ERROR [buggy-app] ZeroDivisionError: division by zero',
            '  File "main.py", line 87, in _handle_data',
        ]

# ── Fake memory store ─────────────────────────────────────────────────────────
class FakeStore:
    def get_recent(self, n=5): return []

# ── Run test ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  Model : {os.environ['OLLAMA_MODEL']}")
    print(f"  Backend: {os.environ['LLM_BACKEND']}")
    print("=" * 60)

    # -- Step 1: Orchestrator diagnosis --
    print("\n[1] Running OrchestratorAgent...")
    t0 = time.monotonic()

    from agents.orchestrator import OrchestratorAgent
    orch = OrchestratorAgent()

    violations = [
        "5xx error rate 95.0% > SLO 1.0%",
        "ZeroDivisionError observed in logs",
    ]
    diag = orch.investigate(
        violations=violations,
        metrics=FakeMetrics(),
        loki=FakeLoki(),
        store=FakeStore(),
        incident_id="test-001",
    )

    elapsed = time.monotonic() - t0
    print(f"   Elapsed    : {elapsed:.1f}s")
    print(f"   Action     : {diag.suggested_actions[0]}")
    print(f"   Severity   : {diag.severity}")
    print(f"   Confidence : {diag.confidence:.0%}")
    print(f"   Root cause : {diag.root_cause}")
    print(f"   Reasoning  : {diag.reasoning}")

    action = diag.suggested_actions[0]
    if action != "patch_code":
        print(f"\n✗ FAIL — expected patch_code, got {action!r}")
        print("  The LLM did not identify the named exception as the trigger.")
        sys.exit(1)

    print("\n✓ Orchestrator correctly diagnosed → patch_code")

    # -- Step 2: CodePatchAgent --
    print("\n[2] Running CodePatchAgent...")
    t1 = time.monotonic()

    from agents.specialists.code_patch import CodePatchAgent
    patch_agent = CodePatchAgent()

    from perception.loki import format_for_llm
    logs_text = format_for_llm(FakeLoki().query_recent_logs())

    proposal = patch_agent.generate(
        root_cause=diag.root_cause,
        severity=diag.severity,
        confidence=diag.confidence,
        recent_logs=logs_text,
        actions_tried=[],
        incident_id="test-001",
    )

    elapsed2 = time.monotonic() - t1
    print(f"   Elapsed    : {elapsed2:.1f}s")

    if proposal is None:
        print("\n✗ FAIL — CodePatchAgent returned None (no patch generated)")
        sys.exit(1)

    print(f"   File       : {proposal.file_path}")
    print(f"   Description: {proposal.description}")
    print(f"   Confidence : {proposal.confidence:.0%}")
    print(f"   Lines Δ    : {proposal.lines_changed}")
    print("\n✓ CodePatchAgent generated a patch")
    print("\n--- Patch preview (first 30 lines) ---")
    for line in proposal.patched_content.splitlines()[:30]:
        print("  " + line)
    if len(proposal.patched_content.splitlines()) > 30:
        print(f"  ... ({len(proposal.patched_content.splitlines())} lines total)")

    print("\n" + "=" * 60)
    print("  RESULT: PASS — full patch_code pipeline works end-to-end")
    print("=" * 60)


if __name__ == "__main__":
    main()
