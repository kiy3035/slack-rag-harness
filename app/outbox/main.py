import asyncio
import signal

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.common.config import get_settings
from app.common.logging import configure_structured_logging
from app.db.session import build_engine
from app.messaging.rabbitmq import RabbitBroker
from app.observability.metrics import start_metrics_server
from app.outbox.publisher import OutboxPublisher
from app.outbox.repository import OutboxRepository


async def async_main() -> None:
    """Outbox Publisher의 연결과 정상 종료 수명 주기를 관리한다."""
    settings = get_settings()
    engine = build_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = OutboxRepository(session_factory)
    broker = RabbitBroker(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    try:
        await broker.connect()
        publisher = OutboxPublisher(repository, broker, settings)
        await publisher.run(stop_event)
    finally:
        await broker.close()
        await engine.dispose()


def main() -> None:
    """동기 Module 진입점에서 비동기 Publisher를 실행한다."""
    settings = get_settings()
    configure_structured_logging("outbox-publisher", settings.log_directory)
    if settings.metrics_enabled:
        start_metrics_server(settings.metrics_port)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

