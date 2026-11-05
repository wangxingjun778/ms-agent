"""python -m service.aigc 的单协调器启动入口。"""

import uvicorn

from service.aigc.app import create_app
from service.aigc.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        timeout_graceful_shutdown=settings.shutdown_timeout,
    )


if __name__ == '__main__':
    main()
