"""Tracing helpers (OpenTelemetry). Without a configured provider every span is a no-op, so
library code can always call `span()`. Never put user query text or payment data in attributes."""
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_tracer = trace.get_tracer("travel_llm")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


def setup_tracing() -> None:
    """Configure export from env: OTEL_TRACES_EXPORTER=console, or OTEL_EXPORTER_OTLP_ENDPOINT
    (needs the `opentelemetry-exporter-otlp` package)."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    exporter = None
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    elif os.getenv("OTEL_TRACES_EXPORTER") == "console":
        exporter = ConsoleSpanExporter()
    if exporter is None:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "travel-llm"}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
