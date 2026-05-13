"""CodePatchAgent — generates source code patches for diagnosed application bugs.

This is a genuinely agentic specialist: it runs its own ReAct loop to read
source files, understand the bug, write a targeted fix, and validate syntax —
before returning a PatchProposal the executor can commit and deploy.

Called when infrastructure remediation (restart, scale, rollback) has either
already failed or when the root cause clearly points to a code bug.
"""
import ast
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import config
from agents.agent_utils import run_tool
from agents.base import Finding, IncidentContext
from agents.langfuse_utils import langfuse_context, observe
from agents.llm import build_llm
from logging_setup import get_logger
from tracing.decisions import get_decision_log
from tracing.spans import agent_span

_log = get_logger("code-patch-agent")

_MAX_ITER = 10


@dataclass
class PatchProposal:
    file_path: str             # relative to app source root
    patched_content: str       # full file content (not a diff)
    description: str           # one-line summary of the fix
    confidence: float          # 0.0–1.0
    lines_changed: int = 0
    old_code: str = ""         # exact lines replaced (for clean-PR re-application)
    new_code: str = ""         # replacement lines


_SYSTEM = """\
You are a senior software engineer fixing a production bug in a Python Flask application.

Tools available:
  list_source_files()
      — list all .py files in the application
  read_function(relative_path, function_name)
      — read ONLY the named function (fast; use this instead of read_source_file)
  read_source_file(relative_path)
      — read the entire file (slow; only if you need more than one function)
  replace_in_file(file_path, old_code, new_code, description, confidence)
      — apply a targeted fix: replace old_code with new_code in the file

Preferred workflow (fastest — use this):
  1. list_source_files()
  2. read_function(file_path, "the_failing_function")
  3. replace_in_file(file_path, <exact buggy lines>, <fixed lines>, description, confidence)

Fallback workflow (only if replace_in_file won't work):
  1. list_source_files()
  2. read_source_file(file_path)
  3. propose_patch(file_path, <COMPLETE new file content>, description, confidence)

Rules for replace_in_file:
- old_code must be an EXACT copy of the lines you want to replace (no paraphrasing).
- new_code is the replacement — can be more or fewer lines.
- The result must be syntactically valid Python.
- Confidence: 0.9 = certain, 0.7 = likely, 0.5 = best-effort fallback.

Common fix patterns:
  - Add `if not collection: return safe_default` before index or division operations
  - Fix wrong multiplier/divisor (e.g. `int(99 * n)` → `int(0.99 * n)`)
  - Wrap in try/except and return a safe value instead of crashing
  - Add an empty-list guard at the top of a function

IMPORTANT: You MUST call replace_in_file or propose_patch — do NOT end without a tool call.
"""

_TASK = """\
Incident root cause:  {root_cause}
Severity:             {severity}
Diagnosis confidence: {diag_confidence:.0%}
Infrastructure actions already tried: {actions_tried}

Recent error logs (stack trace points to the buggy function):
{logs}

Your task:
1. Call list_source_files() to find the file.
2. Call read_function() with the function name from the stack trace.
3. Call replace_in_file() with the exact buggy lines and the fixed replacement.

Use replace_in_file — it is faster than propose_patch and does not require the full file.
"""


class CodePatchAgent:
    """ReAct agent that reads app source and generates a targeted code patch."""

    def __init__(self):
        self._llm      = build_llm(max_tokens=8192)
        self._app_root = Path(config.APP_SOURCE_DIR).resolve()

    @observe(name="code-patch-agent")
    def generate(
        self,
        root_cause: str,
        severity: str,
        confidence: float,
        recent_logs: str,
        actions_tried: list[str],
        incident_id: str = "",
    ) -> PatchProposal | None:
        """Run the ReAct loop and return a PatchProposal, or None if no fix found."""

        langfuse_context.update_current_observation(
            input={
                "root_cause":     root_cause,
                "severity":       severity,
                "actions_tried":  actions_tried,
                "incident_id":    incident_id,
            }
        )

        patch_box: list[PatchProposal] = []
        dlog = get_decision_log()

        # ── Tools (closures over patch_box + self._app_root) ──────────────────

        @tool
        def list_source_files() -> str:
            """List all Python source files in the application directory."""
            try:
                files = sorted(
                    str(p.relative_to(self._app_root))
                    for p in self._app_root.rglob("*.py")
                    if "__pycache__" not in str(p) and ".venv" not in str(p)
                )
                return "\n".join(files) or "No Python files found."
            except Exception as e:
                return f"ERROR: {e}"

        @tool
        def read_source_file(relative_path: str) -> str:
            """Read a source file from the application.
            Args:
                relative_path: path relative to app root, e.g. 'main.py'
            """
            try:
                full = (self._app_root / relative_path).resolve()
                if not str(full).startswith(str(self._app_root)):
                    return "ERROR: path traversal not allowed"
                return full.read_text()
            except FileNotFoundError:
                return f"ERROR: file not found: {relative_path}"
            except Exception as e:
                return f"ERROR: {e}"

        @tool
        def read_function(relative_path: str, function_name: str) -> str:
            """Read ONLY the source of a named function — much faster than reading the whole file.
            Args:
                relative_path: path relative to app root, e.g. 'main.py'
                function_name: exact name of the function or method to read
            """
            try:
                full = (self._app_root / relative_path).resolve()
                if not str(full).startswith(str(self._app_root)):
                    return "ERROR: path traversal not allowed"
                source = full.read_text()
                tree = ast.parse(source)
                lines = source.splitlines()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name == function_name:
                            start = node.lineno - 1
                            end = node.end_lineno
                            snippet = "\n".join(lines[start:end])
                            return f"# {relative_path}:{node.lineno}-{node.end_lineno}\n{snippet}"
                return f"ERROR: function '{function_name}' not found in {relative_path}"
            except Exception as e:
                return f"ERROR: {e}"

        @tool
        def replace_in_file(
            file_path: str,
            old_code: str,
            new_code: str,
            description: str,
            confidence: float,
        ) -> str:
            """Apply a targeted fix by replacing old_code with new_code in the file.
            Args:
                file_path:   path relative to app root (e.g. 'main.py')
                old_code:    exact lines to replace (must appear verbatim in the file)
                new_code:    replacement lines (the fix)
                description: one sentence describing the fix
                confidence:  0.0–1.0
            """
            try:
                full = (self._app_root / file_path).resolve()
                if not str(full).startswith(str(self._app_root)):
                    return "ERROR: path traversal not allowed"
                original = full.read_text()
            except FileNotFoundError:
                return f"REJECTED: file not found: {file_path}"

            if old_code not in original:
                # Try stripping leading/trailing whitespace differences
                stripped_orig = "\n".join(l.rstrip() for l in original.splitlines())
                stripped_old  = "\n".join(l.rstrip() for l in old_code.splitlines())
                if stripped_old not in stripped_orig:
                    return (
                        "REJECTED: old_code not found verbatim in the file. "
                        "Copy the exact lines from read_function() output and retry."
                    )
                patched = stripped_orig.replace(stripped_old, new_code, 1)
            else:
                patched = original.replace(old_code, new_code, 1)

            try:
                ast.parse(patched)
            except SyntaxError as e:
                return f"REJECTED: syntax error after patch — {e}. Fix new_code and retry."

            lines_changed = abs(
                len(patched.splitlines()) - len(original.splitlines())
            ) + len(new_code.splitlines())

            patch_box.append(
                PatchProposal(
                    file_path=file_path,
                    patched_content=patched,
                    description=description,
                    confidence=min(max(float(confidence), 0.0), 1.0),
                    lines_changed=lines_changed,
                    old_code=old_code,
                    new_code=new_code,
                )
            )
            _log.info(
                "replace_in_file accepted",
                extra={
                    "incident_id":   incident_id,
                    "file":          file_path,
                    "lines_changed": lines_changed,
                    "confidence":    confidence,
                },
            )
            return "Fix applied."

        @tool
        def propose_patch(
            file_path: str,
            patched_content: str,
            description: str,
            confidence: float,
        ) -> str:
            """Submit the finished patch.
            Args:
                file_path:        path relative to app root (e.g. 'main.py')
                patched_content:  COMPLETE new content of the file
                description:      one sentence: what was fixed and why
                confidence:       0.0–1.0
            """
            # Syntax validation
            try:
                ast.parse(patched_content)
            except SyntaxError as e:
                return f"REJECTED: syntax error in patch — {e}. Fix and retry."

            # Guard: patched content must not be suspiciously shorter than original
            try:
                original = (self._app_root / file_path).read_text()
                if len(patched_content) < len(original) * 0.6:
                    return (
                        "REJECTED: patched_content is too short — you must provide "
                        "the COMPLETE file, not just the changed section. Retry with the full file."
                    )
                lines_changed = abs(
                    len(patched_content.splitlines()) - len(original.splitlines())
                )
            except FileNotFoundError:
                return f"REJECTED: file not found in app source: {file_path}"
            except Exception:
                lines_changed = 0

            patch_box.append(
                PatchProposal(
                    file_path=file_path,
                    patched_content=patched_content,
                    description=description,
                    confidence=min(max(float(confidence), 0.0), 1.0),
                    lines_changed=lines_changed,
                )
            )
            _log.info(
                "patch proposal accepted",
                extra={
                    "incident_id":   incident_id,
                    "file":          file_path,
                    "lines_changed": lines_changed,
                    "confidence":    confidence,
                },
            )
            return "Patch accepted."

        tools    = [list_source_files, read_function, replace_in_file, read_source_file, propose_patch]
        tool_map = {t.name: t for t in tools}
        llm      = self._llm.bind_tools(tools)

        task = _TASK.format(
            root_cause=root_cause,
            severity=severity,
            diag_confidence=confidence,
            actions_tried=", ".join(actions_tried) if actions_tried else "none",
            logs=recent_logs or "(no recent logs available)",
        )

        messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=task)]

        last_ai_text: str = ""
        nudges_sent: int = 0
        with agent_span("code_patch_agent.react", **{"incident.id": incident_id}):
            for step in range(_MAX_ITER):
                response: AIMessage = llm.invoke(messages)
                messages.append(response)

                if isinstance(response.content, str) and response.content.strip():
                    last_ai_text = response.content

                if not response.tool_calls:
                    # Model described the fix in prose instead of calling propose_patch.
                    # Nudge it once to actually call the tool.
                    if last_ai_text and nudges_sent < 2:
                        nudges_sent += 1
                        _log.debug(
                            f"no tool call — nudging model (nudge {nudges_sent})",
                            extra={"step": step + 1},
                        )
                        messages.append(HumanMessage(
                            content=(
                                "You described the fix but did not call propose_patch(). "
                                "You MUST call propose_patch() now with the COMPLETE fixed file "
                                "content — not just the changed lines, the entire file."
                            )
                        ))
                        continue
                    break

                for tc in response.tool_calls:
                    result = run_tool(tool_map, tc)
                    messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))
                    _log.debug(
                        "tool call",
                        extra={"tool": tc["name"], "result_len": len(result), "step": step + 1},
                    )

                if patch_box:
                    break

        # Fallback: LLM described the fix in prose but never called propose_patch.
        # Try to extract a Python code block from the last AI message.
        if not patch_box and last_ai_text:
            patch_box = self._extract_patch_from_text(last_ai_text, root_cause, incident_id)

        result = patch_box[0] if patch_box else None

        if incident_id:
            dlog.record(
                incident_id,
                "specialist.code_patch",
                f"{'patch ready' if result else 'no patch generated'}"
                + (f": {result.description[:100]}" if result else ""),
                confidence=result.confidence if result else 0.0,
                evidence={
                    "file":     result.file_path if result else "",
                    "lines_Δ":  result.lines_changed if result else 0,
                },
            )

        langfuse_context.update_current_observation(
            output={
                "patched":    bool(result),
                "file":       result.file_path if result else "",
                "confidence": result.confidence if result else 0.0,
            }
        )
        return result

    def _extract_patch_from_text(
        self, text: str, root_cause: str, incident_id: str
    ) -> list[PatchProposal]:
        """Last-resort: parse a fenced Python code block out of the AI's prose response.

        The LLM sometimes produces a valid patch inside ```python ... ``` but doesn't
        call propose_patch. This extracts it, validates syntax, and wraps it as a
        low-confidence PatchProposal so the pipeline can still make progress.
        """
        import re
        blocks = re.findall(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
        for block in blocks:
            try:
                ast.parse(block)
            except SyntaxError:
                continue
            if len(block.splitlines()) < 5:
                continue  # too short to be a real file
            # Try to infer the file path from the text
            path_match = re.search(r"`?([a-zA-Z0-9_/]+\.py)`?", text)
            file_path = path_match.group(1) if path_match else "main.py"
            # Verify it exists in the app root
            if not (self._app_root / file_path).exists():
                file_path = "main.py"

            try:
                original = (self._app_root / file_path).read_text()
            except Exception:
                continue
            if len(block) < len(original) * 0.6:
                continue  # incomplete snippet

            _log.warning(
                "text-extraction fallback: extracted patch from prose response",
                extra={"incident_id": incident_id, "file": file_path, "lines": len(block.splitlines())},
            )
            return [PatchProposal(
                file_path=file_path,
                patched_content=block,
                description=f"fallback patch extracted from text — {root_cause[:80]}",
                confidence=0.45,
                lines_changed=abs(len(block.splitlines()) - len(original.splitlines())),
            )]
        return []

    def as_finding(self, proposal: PatchProposal | None) -> Finding:
        """Convert a PatchProposal to a Finding for the orchestrator to read."""
        if not proposal:
            return Finding(
                agent="code_patch",
                analysis="Could not generate a patch — root cause may need manual investigation.",
                confidence=0.0,
                key_facts=[],
                unexpected=[],
            )
        return Finding(
            agent="code_patch",
            analysis=f"Patch ready for `{proposal.file_path}`: {proposal.description}",
            confidence=proposal.confidence,
            key_facts=[
                f"file: {proposal.file_path}",
                f"lines changed: ±{proposal.lines_changed}",
                f"fix: {proposal.description}",
            ],
            unexpected=[],
        )
