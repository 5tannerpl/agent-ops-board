"""Private control-plane API for authenticated demo operations."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import demo_api
from security.production_config import validate_production_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_config()
    yield


app = FastAPI(title="Agent Ops Board Private API", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(demo_api.private_router)
