import asyncio
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.infrastructure.database import check_database
from app.infrastructure.redis import check_redis

router = APIRouter(tags=["health"])

HealthValue = Literal["healthy", "unhealthy"]


class DependencyHealth(BaseModel):
    api: HealthValue
    database: HealthValue
    redis: HealthValue


class HealthResponse(BaseModel):
    status: HealthValue
    services: DependencyHealth


async def collect_health() -> HealthResponse:
    database_ok, redis_ok = await asyncio.gather(check_database(), check_redis())
    dependencies_ok = database_ok and redis_ok
    return HealthResponse(
        status="healthy" if dependencies_ok else "unhealthy",
        services=DependencyHealth(
            api="healthy",
            database="healthy" if database_ok else "unhealthy",
            redis="healthy" if redis_ok else "unhealthy",
        ),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
)
async def health() -> HealthResponse | JSONResponse:
    result = await collect_health()
    if result.status == "unhealthy":
        return JSONResponse(status_code=503, content=result.model_dump())
    return result
