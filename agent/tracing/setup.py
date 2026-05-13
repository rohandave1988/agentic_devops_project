"""OpenTelemetry tracer initialisation.

Call setup_tracing() once at startup (main.py / test_cycle.py).
Subsequent calls are no-ops — safe to call in tests.

Export targets (mutually exclusive, first wins):
  OTLP_ENDPOINT set  → BatchSpanProcessor → OTLPSpanExporter (gRPC → Jaeger)
  TRACING_CONSOLE    → BatchSpanProcessor → ConsoleSpanExporter (stdout, dev)
  neither            → NoOp — spans exist in memory only, nothing exported
"""
import sys

import config
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_INITIALISED = False


def setup_tracing() -> None:
    """Initialise the global OTel tracer provider. Idempotent."""
    global _INITIALISED
    if _INITIALISED:
        return
    _INITIALISED = True

    resource = Resource({
        "service.name":    config.SERVICE_NAME,
        "service.version": config.SERVICE_VERSION,
        "deployment.environment": "local",
    })

    provider = TracerProvider(resource=resource)

    if config.OTLP_ENDPOINT:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=config.OTLP_ENDPOINT, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _info(f"OTel → OTLP gRPC at {config.OTLP_ENDPOINT}")
        except ImportError:
            _warn("opentelemetry-exporter-otlp-proto-grpc not installed — OTLP export disabled")
    elif config.TRACING_CONSOLE:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        _info("OTel → console (TRACING_CONSOLE=true)")
    else:
        _info("OTel tracing active (no exporter — set OTLP_ENDPOINT or TRACING_CONSOLE=true)")

    trace.set_tracer_provider(provider)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("devops-agent")


def _info(msg: str)  -> None: print(f"[tracing] {msg}", file=sys.stderr)
def _warn(msg: str)  -> None: print(f"[tracing] WARNING: {msg}", file=sys.stderr)
