# Tool definitions exposed to the LLM during the root cause analysis loop.
# Schema format matches the Anthropic API's tool definition structure.

DIAGNOSIS_TOOLS = [
    {
        "name": "get_metrics",
        "description": (
            "Fetch current Kubernetes cluster health metrics from Prometheus. "
            "Returns error rate, P99 latency, CPU/memory usage, pod restarts, "
            "replica counts, and the SLO threshold for each metric."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_logs",
        "description": (
            "Fetch the last 120 seconds of application logs from Loki. "
            "Useful for spotting error messages, stack traces, OOM kills, or unusual patterns."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_incident_history",
        "description": (
            "Retrieve the 5 most recent incident records. Shows anomalies detected, "
            "actions taken, whether SLOs recovered, and MTTR. "
            "Use this to avoid repeating actions that already failed."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_diagnosis",
        "description": (
            "Submit your final root cause diagnosis and recommended remediation. "
            "Call this once you have gathered sufficient evidence. This ends the investigation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root_cause": {
                    "type": "string",
                    "description": "Concise root cause hypothesis based on your investigation",
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score between 0.0 and 1.0",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Step-by-step explanation of how you reached this diagnosis",
                },
                "suggested_actions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["restart_pods", "scale_up", "scale_down", "rollback", "no_action"],
                    },
                    "description": "Ordered list: primary action first, fallback second",
                },
                "anomalies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific anomalies detected (e.g. 'CPU at 91% exceeds 80% SLO')",
                },
            },
            "required": ["root_cause", "severity", "confidence", "suggested_actions", "anomalies"],
        },
    },
]
