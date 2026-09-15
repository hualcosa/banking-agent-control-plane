#!/bin/sh
# One image, two observability backends. `langfuse` (default) lets
# trail.telemetry install its own OTLP exporter, as compose expects. `adot`
# hands the process to opentelemetry-instrument: the ADOT distro installs the
# global TracerProvider before trail.app is imported, trail.telemetry sees an
# empty endpoint and installs nothing, and every `span()` lands in CloudWatch.
set -eu
PORT="${TRAIL_PORT:-8080}"
if [ "${TRAIL_OTEL_MODE:-langfuse}" = "adot" ]; then
  export TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT=""
  exec opentelemetry-instrument uvicorn trail.app:app --host 0.0.0.0 --port "$PORT"
fi
exec uvicorn trail.app:app --host 0.0.0.0 --port "$PORT"
