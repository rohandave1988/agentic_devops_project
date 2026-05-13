"""Human-in-the-Loop review — terminal prompt between Reason and Act.

Called from main.py after a Diagnosis is produced and before the
DecisionEngine selects an action. The operator can approve the AI
recommendation, pick a different action, or let it auto-approve after
timeout_sec seconds of silence.

Also provides review_patch() — a second approval gate shown after
CodePatchAgent generates a fix, before git commit + PR creation.
"""
import difflib
import sys
import threading

from agents.base import Diagnosis
from agentmetrics import metrics as agentmetrics

_VALID_ACTIONS = ["restart_pods", "scale_up", "scale_down", "rollback", "patch_code", "no_action"]


def prompt(diag: Diagnosis, timeout_sec: int) -> str:
    """Display the diagnosis and wait for operator input.

    Returns the chosen canonical action string.
    Auto-approves the AI recommendation after timeout_sec if no input.
    Returns the AI recommendation immediately when stdin is not a TTY
    (container, CI, piped input) to avoid blocking indefinitely.
    """
    ai_action = diag.suggested_actions[0] if diag.suggested_actions else "no_action"
    if not sys.stdin.isatty():
        print(f"\n[HITL] stdin is not a TTY — auto-approving: {ai_action}")
        return ai_action
    border    = "═" * 66

    print(f"\n{border}")
    print("  ⚑  HUMAN REVIEW  —  approve or override before action executes")
    print(border)
    print(f"  Root Cause  : {diag.root_cause}")
    print(f"  Severity    : {diag.severity.upper()}")
    print(f"  Confidence  : {diag.confidence * 100:.0f}%")
    if diag.reasoning:
        print(f"  Reasoning   : {diag.reasoning}")
    if diag.anomalies:
        print("  Anomalies   :")
        for a in diag.anomalies[:5]:
            print(f"    • {a}")
    print()
    print(f"  AI recommendation → [{ai_action}]")
    print()
    print("  Choose action  (Enter = accept AI recommendation):")
    for i, action in enumerate(_VALID_ACTIONS, 1):
        marker = "  ◄ AI" if action == ai_action else ""
        print(f"    {i}. {action}{marker}")
    print(f"\n  [Auto-approves AI recommendation in {timeout_sec}s]")
    print("  > ", end="", flush=True)

    chosen = [ai_action]

    def _read():
        try:
            line = sys.stdin.readline().strip()
            if not line:
                return
            if line.isdigit():
                idx = int(line) - 1
                if 0 <= idx < len(_VALID_ACTIONS):
                    chosen[0] = _VALID_ACTIONS[idx]
            elif line.lower() in _VALID_ACTIONS:
                chosen[0] = line.lower()
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    final = chosen[0]
    if final == ai_action:
        timed_out = chosen[0] == ai_action and not t.is_alive()
        outcome = "timeout" if timed_out else "accepted"
        print(f"\n  ✓  Approved: {final}")
    else:
        outcome = "overridden"
        print(f"\n  ✎  Human override: {ai_action} → {final}")
    agentmetrics.HITL_DECISIONS.labels(outcome=outcome).inc()
    print(f"{border}\n")
    return final


def review_patch(
    file_path: str,
    original: str,
    patched: str,
    description: str,
    confidence: float,
    timeout_sec: int,
) -> bool:
    """Show the generated diff and ask the operator to approve before push.

    Returns True  → proceed with commit + PR.
    Returns False → discard the patch, take no action.
    Auto-approves (True) when stdin is not a TTY.
    """
    if not sys.stdin.isatty():
        print(f"\n[HITL] stdin is not a TTY — auto-approving patch for {file_path}")
        return True

    border = "═" * 66
    orig_lines    = original.splitlines(keepends=True)
    patched_lines = patched.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        orig_lines, patched_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    ))

    print(f"\n{border}")
    print("  ⚑  PATCH REVIEW  —  approve before commit + PR")
    print(border)
    print(f"  File        : {file_path}")
    print(f"  Fix         : {description}")
    print(f"  Confidence  : {confidence * 100:.0f}%")
    print(f"  Lines in diff: {len(diff)}")
    print()

    # Colour the unified diff
    RED   = "\033[31m"
    GREEN = "\033[32m"
    CYAN  = "\033[36m"
    RESET = "\033[0m"

    if diff:
        print("  ── diff ────────────────────────────────────────────────────")
        for line in diff[:120]:          # cap at 120 lines so terminal stays readable
            line_stripped = line.rstrip("\n")
            if line_stripped.startswith("---") or line_stripped.startswith("+++"):
                print(f"  {CYAN}{line_stripped}{RESET}")
            elif line_stripped.startswith("-"):
                print(f"  {RED}{line_stripped}{RESET}")
            elif line_stripped.startswith("+"):
                print(f"  {GREEN}{line_stripped}{RESET}")
            elif line_stripped.startswith("@@"):
                print(f"  {CYAN}{line_stripped}{RESET}")
            else:
                print(f"  {line_stripped}")
        if len(diff) > 120:
            print(f"  … ({len(diff) - 120} more lines)")
        print("  ────────────────────────────────────────────────────────────")
    else:
        print("  (no diff — patch identical to original)")

    print()
    print("  [y] Approve — commit, push, open PR")
    print("  [n] Reject  — discard patch, take no action")
    print(f"\n  [Auto-approves in {timeout_sec}s]")
    print("  > ", end="", flush=True)

    decision = [True]   # default: approve

    def _read():
        try:
            line = sys.stdin.readline().strip().lower()
            if line in ("n", "no", "reject"):
                decision[0] = False
            # anything else (y, yes, enter) → approve
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if decision[0]:
        print(f"\n  ✓  Patch approved — proceeding with commit + PR")
    else:
        print(f"\n  ✗  Patch rejected — discarding fix")
    print(f"{border}\n")
    return decision[0]
