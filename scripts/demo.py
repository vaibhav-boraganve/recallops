"""Terminal walkthrough of the RecallOps learning curve.

  Act 1  A payments-api alert fires. Triage with no memory vs. with Hindsight memory.
  Act 2  A brand-new kind of outage (expired TLS cert). The agent has nothing to go on.
         The on-call fixes it and RecallOps retains the resolution.
  Act 3  A week later the same failure hits a different service. RecallOps now recalls it.

Usage: uv run python scripts/demo.py   (run scripts/seed_memory.py first)
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recallops.agent import RecallOpsAgent, TriageResult  # noqa: E402
from recallops.config import load_settings  # noqa: E402
from recallops.data import DEMO_ALERTS, DEMO_NEW_RESOLUTION  # noqa: E402
from recallops.llm import build_llm  # noqa: E402
from recallops.memory import build_memory  # noqa: E402

ALERTS = {a.id: a for a in DEMO_ALERTS}


def banner(text: str) -> None:
    print("\n" + "=" * 88 + f"\n{text}\n" + "=" * 88)


def show(label: str, result: TriageResult) -> None:
    print(f"\n--- {label} ---")
    print(f"memories recalled: {len(result.recalled)}   cited incidents: {', '.join(result.cited_incidents) or 'none'}")
    print(textwrap.indent(result.brief, "  "))


async def main() -> None:
    settings = load_settings()
    memory = build_memory(settings)
    agent = RecallOpsAgent(memory, build_llm(settings))
    await memory.ensure_bank()

    try:
        banner("ACT 1  ALERT-7781: payments-api 5xx after deploy. Same alert, with and without memory.")
        alert = ALERTS["ALERT-7781"]
        without, with_memory = await asyncio.gather(
            agent.triage(alert, use_memory=False), agent.triage(alert, use_memory=True)
        )
        show("Generic LLM (no memory)", without)
        show(f"RecallOps ({memory.name} memory)", with_memory)

        banner("ACT 2  ALERT-7815: checkout-web cert expired. Something the team has never seen.")
        first = await agent.triage(ALERTS["ALERT-7815"])
        show("RecallOps, first encounter", first)
        print(f"\nOn-call resolves it in {DEMO_NEW_RESOLUTION.minutes_to_resolve} min. Root cause: {DEMO_NEW_RESOLUTION.root_cause}")
        await agent.record_resolution(DEMO_NEW_RESOLUTION)
        print(f"Retained {DEMO_NEW_RESOLUTION.id} into {memory.name} memory bank '{settings.bank_id}'.")
        if memory.name == "hindsight":
            print("Waiting a few seconds for Hindsight to finish indexing...")
            await asyncio.sleep(5)

        banner("ACT 3  One week later. ALERT-7840: fraud-scoring cert expired. Different service, same failure.")
        second = await agent.triage(ALERTS["ALERT-7840"])
        show("RecallOps, after learning", second)
        learned = DEMO_NEW_RESOLUTION.id in second.cited_incidents or any(
            DEMO_NEW_RESOLUTION.id in (h.text + (h.document_id or "")) for h in second.recalled
        )
        print(f"\nRecalled the incident it learned in Act 2: {'YES' if learned else 'no'}")
    finally:
        await memory.close()


if __name__ == "__main__":
    asyncio.run(main())
