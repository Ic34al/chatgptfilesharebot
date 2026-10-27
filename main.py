import argparse
import asyncio

from bot import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ANIME SOUL bot")
    parser.add_argument("--role", choices=["all", "bot", "pipeline"], default=None,
                        help="Override the ROLE environment variable")
    args = parser.parse_args()
    asyncio.run(run(args.role))
