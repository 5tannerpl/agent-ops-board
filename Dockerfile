FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
    
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --requirement requirements.lock
RUN python -c "from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse; print('OTLP import OK')"

COPY api ./api
COPY crosscloud ./crosscloud
COPY dashboard ./dashboard
COPY security ./security
COPY storage ./storage
COPY intake ./intake
COPY loadtest ./loadtest
COPY workers ./workers
COPY scaling ./scaling
COPY orchestration ./orchestration
COPY adapters ./adapters
COPY config.py demo_api.py demo_auth.py demo_scaling.py demo_service.py demo_store.py kaggle_launcher.py otlp_api.py otlp_store.py otlp_semantics.py trace_store.py ./
RUN python -c "import api.private_app, api.public_ingress, opentelemetry.sdk.trace"

RUN addgroup --system --gid 10001 aob \
    && adduser --system --uid 10001 --ingroup aob aob \
    && mkdir -p /var/lib/aob /app/.streamlit /home/aob \
    && chown -R aob:aob /app /var/lib/aob /home/aob

ENV HOME=/home/aob

USER aob
EXPOSE 8010 8011

CMD ["uvicorn", "api.private_app:app", "--host", "0.0.0.0", "--port", "8010"]
