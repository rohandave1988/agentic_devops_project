"""SpecialistAgent — shared ReAct loop for all domain specialists.

Subclasses provide: AGENT_NAME, SPAN_NAME, MAX_ITER, system prompt, and
a domain_tools(ctx) factory. The loop, memory, tracing, and Langfuse
wiring are handled here.
"""
from abc import ABC, abstractmethod

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

import config
from agents.agent_utils import extract_text, run_tool
from agents.base import Finding, IncidentContext
from agents.langfuse_utils import langfuse_context, observe
from agents.llm import build_llm
from agents.memory import AgentMemory
from logging_setup import get_logger
from tracing.decisions import get_decision_log
from tracing.spans import agent_span, set_span_attrs


class SpecialistAgent(ABC):
    """Base class for MetricsAgent, LogsAgent, HistoryAgent."""

    AGENT_NAME: str   # e.g. "metrics"
    SPAN_NAME:  str   # e.g. "specialist.metrics"
    MAX_ITER:   int = 6
    SYSTEM:     str = ""

    def __init__(self):
        self._llm    = build_llm()
        self._memory = AgentMemory(self.AGENT_NAME)
        self._log    = get_logger(f"agent.{self.AGENT_NAME}")

    @abstractmethod
    def domain_tools(self, ctx: IncidentContext) -> list:
        """Return domain-specific LangChain tools for this agent."""

    def _observe(self, fn):
        """Wrap run() with the correct agent name for Langfuse."""
        return observe(name=f"{self.AGENT_NAME}-agent")(fn)

    def run(self, ctx: IncidentContext, question: str) -> Finding:
        _model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.CLAUDE_MODEL
        name   = self.AGENT_NAME

        with agent_span(
            self.SPAN_NAME,
            **{
                "incident.id":  ctx.incident_id,
                "question":     question[:200],
                "llm.model":    _model,
                "llm.provider": config.LLM_BACKEND,
            },
        ) as sp:
            finding_box: list[Finding] = []

            @tool
            def submit_finding(
                analysis: str,
                confidence: float,
                key_facts: list[str],
                unexpected: list[str],
            ) -> str:
                """Submit your structured finding to the orchestrator.

                Args:
                    analysis:   Direct answer to the question with specific values
                    confidence: How certain you are, 0.0–1.0
                    key_facts:  Concrete extracted facts as short strings
                    unexpected: Anomalies noticed beyond the question asked
                """
                conf = min(max(float(confidence), 0.0), 1.0)
                finding_box.append(Finding(
                    agent=name,
                    analysis=analysis,
                    confidence=conf,
                    key_facts=key_facts or [],
                    unexpected=unexpected or [],
                ))
                get_decision_log().record(
                    ctx.incident_id,
                    f"specialist.{name}",
                    f"finding: {analysis[:120]}",
                    reasoning="; ".join((key_facts or [])[:3]),
                    evidence={
                        "key_facts":  (key_facts or [])[:4],
                        "unexpected": (unexpected or [])[:2],
                    },
                    confidence=conf,
                )
                return "Finding recorded."

            all_tools = self.domain_tools(ctx) + [submit_finding]
            tool_map  = {t.name: t for t in all_tools}
            llm       = self._llm.bind_tools(all_tools)

            memory_ctx = self._memory.recall()
            task       = f"{question}\n\n{memory_ctx}" if memory_ctx else question
            messages   = [SystemMessage(content=self.SYSTEM), HumanMessage(content=task)]
            iterations = 0

            langfuse_context.update_current_observation(
                input={"question": question, "incident_id": ctx.incident_id},
            )
            self._log.info(f"investigating: {question[:120]}", extra={"incident_id": ctx.incident_id})

            for _ in range(self.MAX_ITER):
                iterations += 1
                with agent_span(
                    "llm.invoke",
                    **{"llm.model": _model, "llm.provider": config.LLM_BACKEND, "iteration": iterations},
                ):
                    response: AIMessage = llm.invoke(messages)

                messages.append(response)
                if not response.tool_calls:
                    break
                done = False
                for tc in response.tool_calls:
                    messages.append(ToolMessage(content=run_tool(tool_map, tc), tool_call_id=tc["id"]))
                    if tc["name"] == "submit_finding":
                        done = True
                if done and finding_box:
                    break

            f = finding_box[0] if finding_box else Finding(
                agent=name,
                analysis=extract_text(messages) or "(no analysis produced)",
                confidence=0.3,
                key_facts=[],
                unexpected=[],
            )

            set_span_attrs(sp, **{
                "finding.confidence":       f.confidence,
                "finding.unexpected_count": len(f.unexpected),
                "iterations":               iterations,
            })

        langfuse_context.update_current_observation(
            output={"analysis": f.analysis[:200], "confidence": f.confidence, "key_facts": f.key_facts[:4]},
        )
        self._memory.remember(ctx.incident_id, question, f.key_facts, f.unexpected)
        self._log.info(
            f"finding ready — confidence={f.confidence:.0%} | facts={len(f.key_facts)} | {f.analysis[:100]}",
            extra={"incident_id": ctx.incident_id, "confidence": round(f.confidence, 2), "key_facts": len(f.key_facts), "unexpected": len(f.unexpected)},
        )
        return f
