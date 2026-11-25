"""Private control-plane API for authenticated demo operations."""
from contextlib import asynccontextmanager
from contextlib import closing

from fastapi import FastAPI

import demo_api
import demo_store
import config
from api import deployments
from crosscloud import store
from security.production_config import validate_production_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_config()
    if config.CROSS_CLOUD_ENABLED:
        with closing(demo_store.connect()) as conn:
            conn.executescript(store.SCHEMA)
    yield


def healthz():
    conn = demo_store.connect()
    conn.close()
    return {"status": "ok"}


def create_app():
    app = FastAPI(title="Agent Ops Board Private API", version="1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(demo_api.private_router)
    app.get("/healthz", include_in_schema=False)(healthz)
    if config.CROSS_CLOUD_ENABLED:
        app.mount("/api/v1", deployments.app)
    return app


app = create_app()
