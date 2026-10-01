import argparse
import asyncio
import logging

from dotenv import load_dotenv

from jobwatch.config import load_config
from jobwatch.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobwatch")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(load_config(args.config), once=args.once))


if __name__ == "__main__":
    main()
