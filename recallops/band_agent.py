"""RecallOps as a Band agent.

Run it, add "RecallOps" to a Band room (e.g. #inc-war-room), and @mention it with an alert.
The agent uses the same Hindsight memory bank as the dashboard, so a resolution recorded in the
room is recalled by the dashboard and vice versa.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from band import Agent, LogSettings
from band.adapters import LangGraphAdapter
from band.adapters.langgraph import LangGraphAdapterConfig

from .agent import RecallOpsAgent
from .config import Settings, load_settings
from .data import Alert, Incident
from .llm import build_llm
from .memory import MemoryUnavailableError, build_memory

INSTRUCTIONS = """You are RecallOps, the on-call incident copilot for Acme Pay's SRE team, working inside a
Band incident room alongside human responders.

When someone pastes an alert, error logs, or describes an outage: call `triage_alert` and post the brief.
When someone says an incident is resolved or explains the fix: collect incident id (INC-YYYY-NNNN), service,
severity, symptoms, root cause, fix steps and what did not work (ask once for anything missing), then call
`record_resolution` so the team never has to rediscover it.
When someone says a suggestion worked or did not: call `record_feedback`.
When asked "have we seen this before" or about past incidents: call `search_incident_memory`.
Never invent past incident IDs; only cite what the tools return. Keep replies short and scannable."""


def build_tools(agent: RecallOpsAgent):
    @tool
    async def triage_alert(title: str, service: str, details: str = "", severity: str = "sev2") -> str:
        """Triage a production alert using the team's incident memory. Returns a brief with likely cause,
        next steps, things to avoid and similar past incidents. severity is one of sev1..sev4."""
        alert = Alert(id="BAND-ALERT", title=title, service=service, severity=severity, details=details)
        result = await agent.triage(alert, use_memory=True)
        note = f"\n\n_Recalled {len(result.recalled)} memories from Hindsight._" if result.recalled else ""
        return result.brief + note

    @tool
    async def record_resolution(
        incident_id: str,
        title: str,
        service: str,
        severity: str,
        symptoms: str,
        root_cause: str,
        fix_steps: list[str],
        did_not_work: list[str] | None = None,
        minutes_to_resolve: int = 0,
        responder: str = "on-call",
    ) -> str:
        """Retain a resolved incident into long-term team memory (Hindsight). incident_id looks like INC-2026-1108."""
        incident = Incident(
            id=incident_id,
            title=title,
            service=service,
            severity=severity,
            occurred_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            responder=responder,
            symptoms=symptoms,
            root_cause=root_cause,
            fix_steps=fix_steps,
            did_not_work=did_not_work or [],
            minutes_to_resolve=minutes_to_resolve,
        )
        try:
            await agent.record_resolution(incident)
        except MemoryUnavailableError as exc:
            return f"Could not save to memory: {exc}"
        return f"Saved {incident_id} to team memory. Next time this happens I will recall it."

    @tool
    async def record_feedback(alert_title: str, service: str, helpful: bool, notes: str = "") -> str:
        """Record whether a RecallOps suggestion actually helped, so future triage learns from it."""
        alert = Alert(id="BAND-ALERT", title=alert_title, service=service, severity="sev2", details="")
        try:
            await agent.record_feedback(alert, helpful=helpful, notes=notes)
        except MemoryUnavailableError as exc:
            return f"Could not save feedback: {exc}"
        return "Feedback saved to team memory."

    @tool
    async def search_incident_memory(query: str) -> str:
        """Search the team's incident memory for past incidents, fixes and feedback."""
        try:
            hits = await agent.memory.recall(query, limit=6)
        except MemoryUnavailableError as exc:
            return f"Memory unavailable: {exc}"
        if not hits:
            return "Nothing in team memory matches that."
        return "\n".join(f"- [{h.occurred_at or '?'}] {h.text}" for h in hits)

    return [triage_alert, record_resolution, record_feedback, search_incident_memory]


def build_band_agent(settings: Settings) -> Agent:
    if not settings.llm_enabled:
        raise SystemExit("GROQ_API_KEY is required to run the Band agent.")
    agent_id = os.getenv("BAND_AGENT_ID")
    api_key = os.getenv("BAND_API_KEY")
    if not agent_id or not api_key:
        raise SystemExit("Set BAND_AGENT_ID and BAND_API_KEY (create a remote agent in the Band app first).")

    core = RecallOpsAgent(build_memory(settings), build_llm(settings))
    llm = ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=0.2,
        max_retries=3,
    )
    adapter = LangGraphAdapter(
        LangGraphAdapterConfig(custom_section=INSTRUCTIONS),
        llm=llm,
        checkpointer=InMemorySaver(),
        additional_tools=build_tools(core),
    )
    return Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)


async def main() -> None:
    LogSettings().for_application().configure()
    settings = load_settings()
    agent = build_band_agent(settings)
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
