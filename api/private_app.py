"""Private control-plane API for authenticated demo operations."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import demo_api
import demo_store
from security.production_config import validate_production_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_config()
    yield


app = FastAPI(title="Agent Ops Board Private API", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(demo_api.private_router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    conn = demo_store.connect()
    conn.close()
    return {"status": "ok"}
