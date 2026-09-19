import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from app.api.routes import router
from app.db.session import engine


logger = logging.getLogger("app.request")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """애플리케이션 종료 시 DB 연결 풀을 안전하게 해제한다."""
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    """라우팅과 요청 추적이 구성된 FastAPI 애플리케이션을 생성한다."""
    application = FastAPI(title="Slack RAG Harness", version="0.1.0", lifespan=lifespan)
    application.include_router(router)

    @application.middleware("http")
    async def add_request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """본문을 기록하지 않고 요청 식별자와 처리 시간만 관측한다."""
        request_id = request.headers.get("x-request-id", str(uuid4()))
        started_at = perf_counter()
        response = await call_next(request)
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    return application


app = create_app()
