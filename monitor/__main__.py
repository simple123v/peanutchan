import asyncio
import logging
import signal

from .bot import AvatarBot
from .config import Config


async def main():
    config = Config.from_env()
    async with AvatarBot(config) as bot:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, lambda: asyncio.create_task(bot.close()))
            except (NotImplementedError, RuntimeError):
                pass  # Windows uses asyncio.run's normal KeyboardInterrupt path.
        await bot.start(config.token)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP debug logs can expose authentication information; never enable them here.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
