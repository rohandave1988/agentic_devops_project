"""LLM client — multi-turn tool-use loop for Claude and Ollama.

The LLM calls investigation tools freely, then calls submit_diagnosis
to return its structured finding. The Anthropic SDK handles the Claude
protocol natively; Ollama uses the OpenAI-compatible chat endpoint.
"""
import json
import logging
import time
from dataclasses import dataclass

import anthropic
import requests

import config
from agentmetrics import metrics as agentmetrics

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8


@dataclass
class Diagnosis:
    anomalies: list[str]
    root_cause: str
    severity: str           # critical | high | medium | low
    suggested_actions: list[str]
    confidence: float       # 0.0 – 1.0
    reasoning: str = ""


class LLMClient:
    def __init__(self):
        self._anthropic = (
            anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)
            if config.ANTHROPIC_KEY else None
        )

    def complete_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        runner,
    ) -> Diagnosis:
        backend = config.LLM_BACKEND
        start = time.time()
        try:
            if backend == "claude":
                diag = self._claude_tool_loop(system_prompt, user_message, tools, runner)
            elif backend == "ollama":
                diag = self._ollama_tool_loop(system_prompt, user_message, tools, runner)
            else:
                raise ValueError(f"Unknown LLM_BACKEND: {backend}")
            agentmetrics.LLM_LATENCY.labels(backend=backend).observe(time.time() - start)
            agentmetrics.LLM_CALLS.labels(backend=backend, result="success").inc()
            return diag
        except Exception:
            agentmetrics.LLM_LATENCY.labels(backend=backend).observe(time.time() - start)
            agentmetrics.LLM_CALLS.labels(backend=backend, result="error").inc()
            raise

    # ── Claude ────────────────────────────────────────────────────────────────

    def _claude_tool_loop(self, system: str, user: str, tools: list, runner) -> Diagnosis:
        messages = [{"role": "user", "content": user}]

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = self._anthropic.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=4096,
                system=system,
                tools=tools,
                messages=messages,
            )
            logger.debug(f"claude turn {iteration + 1}: stop_reason={response.stop_reason}")

            # Append assistant turn to conversation history
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info(f"LLM called tool: {block.name}")
                if block.name == "submit_diagnosis":
                    return _diagnosis_from_dict(block.input)
                result = runner.execute(block.name, block.input)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                break

        raise RuntimeError(
            f"LLM did not call submit_diagnosis within {MAX_TOOL_ITERATIONS} iterations"
        )

    # ── Ollama (OpenAI-compatible) ────────────────────────────────────────────

    def _ollama_tool_loop(self, system: str, user: str, tools: list, runner) -> Diagnosis:
        # Convert to Ollama/OpenAI tool format
        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name":        t["name"],
                    "description": t["description"],
                    "parameters":  t["input_schema"],
                },
            }
            for t in tools
        ]

        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]

        for iteration in range(MAX_TOOL_ITERATIONS):
            resp = requests.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={
                    "model":    config.OLLAMA_MODEL,
                    "messages": messages,
                    "tools":    ollama_tools,
                    "stream":   False,
                },
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["message"]
            tool_calls = msg.get("tool_calls") or []
            logger.debug(f"ollama turn {iteration + 1}: tool_calls={len(tool_calls)}")

            messages.append(msg)

            if not tool_calls:
                break

            for tc in tool_calls:
                name = tc["function"]["name"]
                args = tc["function"].get("arguments", {})
                # Ollama may encode arguments as a JSON string or as a dict
                if isinstance(args, str):
                    args = json.loads(args)
                logger.info(f"LLM called tool: {name}")
                if name == "submit_diagnosis":
                    return _diagnosis_from_dict(args)
                result = runner.execute(name, args)
                messages.append({
                    "role":         "tool",
                    "content":      result,
                    "tool_call_id": tc.get("id", ""),
                })

        raise RuntimeError(
            f"LLM did not call submit_diagnosis within {MAX_TOOL_ITERATIONS} iterations"
        )


def _diagnosis_from_dict(d: dict) -> Diagnosis:
    return Diagnosis(
        anomalies=d.get("anomalies", []),
        root_cause=d.get("root_cause", ""),
        severity=d.get("severity", "medium"),
        suggested_actions=d.get("suggested_actions", []),
        confidence=float(d.get("confidence", 0.0)),
        reasoning=d.get("reasoning", ""),
    )
