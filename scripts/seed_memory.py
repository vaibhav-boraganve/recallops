"""Retain Acme Pay's incident history into the Hindsight memory bank.

Usage: uv run python scripts/seed_memory.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recallops.agent import RecallOpsAgent  # noqa: E402
from recallops.config import load_settings  # noqa: E402
from recallops.data import HISTORICAL_INCIDENTS  # noqa: E402
from recallops.memory import MemoryUnavailableError, build_memory  # noqa: E402


async def main() -> int:
    settings = load_settings()
    memory = build_memory(settings)
    agent = RecallOpsAgent(memory, llm=None)
    print(f"Seeding {len(HISTORICAL_INCIDENTS)} incidents into {memory.name} bank '{settings.bank_id}'...")
    try:
        await memory.ensure_bank()
        for incident in HISTORICAL_INCIDENTS:
            await agent.record_resolution(incident)
            print(f"  retained {incident.id}  {incident.service:<20} {incident.title[:60]}")
    except MemoryUnavailableError as exc:
        print(f"\nFailed: {exc}\nIs Hindsight running at {settings.hindsight_url}? See README step 2.", file=sys.stderr)
        return 1
    finally:
        await memory.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
