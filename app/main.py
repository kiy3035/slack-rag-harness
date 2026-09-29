import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import os
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from app.api.routes import router
from app.common.logging import configure_structured_logging
from app.db.session import engine
from app.observability.api import router as observability_router
from app.observability.metrics import record_api_request


logger = logging.getLogger("app.request")
configure_structured_logging(
    service_name=os.getenv("SERVICE_NAME", "api"),
    log_directory=os.getenv("LOG_DIRECTORY"),
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """애플리케이션 종료 시 DB 연결 풀을 안전하게 해제한다."""
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    """라우팅과 요청 추적이 구성된 FastAPI 애플리케이션을 생성한다."""
    application = FastAPI(title="Slack RAG Harness", version="0.1.0", lifespan=lifespan)
    application.include_router(router)
    application.include_router(observability_router)

    @application.middleware("http")
    async def add_request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """본문을 기록하지 않고 요청 식별자와 처리 시간만 관측한다."""
        request_id = request.headers.get("x-request-id", str(uuid4()))
        started_at = perf_counter()
        response: Response | None = None
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration_seconds = perf_counter() - started_at
            route_object = request.scope.get("route")
            route_path = getattr(route_object, "path", "__unmatched__")
            record_api_request(
                method=request.method,
                route=route_path,
                status_code=status_code,
                duration_seconds=duration_seconds,
            )
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "route": route_path,
                    "status_code": status_code,
                    "duration_ms": round(duration_seconds * 1_000, 2),
                },
            )

    return application


app = create_app()
