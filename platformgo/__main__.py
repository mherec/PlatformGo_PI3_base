"""Entry point: python -m platformgo"""
import asyncio
import logging
import os
import signal
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from . import sounds
from .actions import Actions
from .cloud import Cloud
from .config import Config
from .hub import Hub
from .web import make_app
from .workers.audio import AudioWorker
from .workers.ble import BleWorker
from .workers.gpio import GpioWorker
from .workers.leds import LedWorker
from .workers.uart import UartWorker

log = logging.getLogger("platformgo")


async def main():
    data_dir = Path(os.environ.get("PG_DATA_DIR", "/var/lib/platformgo"))
    port = int(os.environ.get("HTTP_PORT", "8080"))
    loop = asyncio.get_running_loop()

    config = Config(data_dir / "config.json")
    hub = Hub(loop)
    sounds.ensure(data_dir / "sounds")

    workers = {
        "leds": LedWorker(hub, config),
        "gpio": GpioWorker(hub, config),
        "uart": UartWorker(hub, config),
        "audio": AudioWorker(hub, config, data_dir / "sounds"),
    }
    actions = Actions(workers, config, loop)
    workers["ble"] = BleWorker(hub, config, actions.run_threadsafe)
    cloud = Cloud(hub, actions)
    for w in workers.values():
        w.start()

    ctx = SimpleNamespace(config=config, hub=hub, workers=workers, actions=actions, cloud=cloud)
    runner = web.AppRunner(make_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("listening on :%d", port)

    tasks = [asyncio.create_task(hub.run()), asyncio.create_task(cloud.run())]
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:     # Windows dev run: Ctrl+C raises KeyboardInterrupt
            pass
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for w in workers.values():
            w.stop()
        for w in workers.values():
            await asyncio.to_thread(w.join, 3)
        for t in tasks:
            t.cancel()
        await cloud.close()
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
