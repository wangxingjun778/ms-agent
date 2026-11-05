"""python -m service.aigc 的单协调器启动入口。"""

import uvicorn

from service.aigc.app import create_app
from service.aigc.settings import Settings


class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig, frame):
        runtime = getattr(self.config.app.state, 'runs', None)
        if runtime:
            runtime.begin_drain()
        super().handle_exit(sig, frame)


def main() -> None:
    settings = Settings.load()
    server = settings.server
    configuration = uvicorn.Config(
        create_app(settings),
        host=server.host,
        port=server.port,
        workers=server.workers,
        timeout_keep_alive=server.keep_alive_timeout,
        timeout_graceful_shutdown=server.graceful_shutdown_timeout,
        limit_concurrency=server.limit_concurrency,
        loop=server.loop,
        http=server.http,
        access_log=server.access_log,
    )
    DrainingServer(configuration).run()


if __name__ == '__main__':
    main()
