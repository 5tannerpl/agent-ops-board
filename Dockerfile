FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock .
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY api ./api
COPY security ./security
COPY storage ./storage
COPY intake ./intake
COPY loadtest ./loadtest
COPY workers ./workers
COPY scaling ./scaling
COPY orchestration ./orchestration
COPY adapters ./adapters
COPY config.py demo_api.py demo_auth.py demo_scaling.py demo_service.py demo_store.py kaggle_launcher.py ./

RUN addgroup --system --gid 10001 aob \
    && adduser --system --uid 10001 --ingroup aob aob \
    && mkdir -p /var/lib/aob \
    && chown -R aob:aob /app /var/lib/aob

USER aob
EXPOSE 8010 8011

CMD ["uvicorn", "api.private_app:app", "--host", "0.0.0.0", "--port", "8010"]
