"""The RecallOps triage agent: recall -> reflect -> brief, and retain -> learn."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .data import Alert, Incident, incident_tags
from .llm import ChatLLM, LLMError
from .memory import IncidentMemory, MemoryHit, MemoryUnavailableError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are RecallOps, the on-call incident copilot for Acme Pay's SRE team.
Services: payments-api (Java/Spring + Postgres via PgBouncer), ledger-service (Kafka consumer),
checkout-web (Next.js behind nginx ingress + CloudFront), fraud-scoring (Python model server),
notification-worker (Celery + Redis). Everything runs on EKS and deploys via Argo CD.

Write a triage brief for the responder in Markdown with exactly these sections:
### Likely cause
### Do this now
(numbered, at most 4 steps, include exact commands when you know them)
### Avoid
(things that wasted time in the past or are likely red herrings)
### Similar past incidents
### Confidence
(High / Medium / Low with one sentence why)

Rules:
- Only cite incident IDs and facts that appear in TEAM MEMORY. Never invent incident IDs.
- If TEAM MEMORY is empty, or nothing in it shares the same failure mode, say this looks new to the
  team, give sound generic SRE steps, and write "None on record" under Similar past incidents.
  Do not force a match from an incident that merely shares a service name or a generic word.
- Be terse. The responder is reading this at 3am."""

ID_PATTERN = re.compile(r"INC-\d{4}-\d{4}")


@dataclass
class TriageResult:
    alert: dict
    used_memory: bool
    brief: str
    recalled: list[MemoryHit] = field(default_factory=list)
    insight: str | None = None
    cited_incidents: list[str] = field(default_factory=list)
    llm_used: bool = False
    memory_error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["recalled"] = [hit.to_dict() for hit in self.recalled]
        return data


def alert_query(alert: Alert) -> str:
    return f"{alert.service} {alert.severity} incident: {alert.title}\n{alert.details}"


def format_memory(hits: list[MemoryHit], insight: str | None) -> str:
    if not hits and not insight:
        return "(empty)"
    parts = []
    for hit in hits:
        source = f" (source: {hit.document_id})" if hit.document_id and ID_PATTERN.fullmatch(hit.document_id) else ""
        parts.append(f"- [{hit.occurred_at or 'unknown date'}]{source} {hit.text}")
    if insight:
        parts.append(f"\nPattern analysis across past incidents (Hindsight reflect):\n{insight}")
    return "\n".join(parts)


class RecallOpsAgent:
    def __init__(self, memory: IncidentMemory, llm: ChatLLM | None) -> None:
        self.memory = memory
        self.llm = llm

    async def triage(self, alert: Alert, *, use_memory: bool = True) -> TriageResult:
        hits: list[MemoryHit] = []
        insight: str | None = None
        memory_error: str | None = None
        if use_memory:
            query = alert_query(alert)
            try:
                hits = await self.memory.recall(query, limit=8)
            except MemoryUnavailableError as exc:
                log.warning("Recall failed, triaging without memory: %s", exc)
                memory_error = str(exc)
            if hits:
                insight = await self.memory.reflect(
                    f"A new alert fired: {alert.title}. Details: {alert.details}\n"
                    "Which past incidents are most similar, what was the root cause and fix each time, "
                    "and what should the responder avoid doing?"
                )

        memory_block = format_memory(hits, insight)
        brief, llm_used = await self._write_brief(alert, memory_block, hits)
        known_ids = set(ID_PATTERN.findall(memory_block))
        cited = sorted({i for i in ID_PATTERN.findall(brief) if i in known_ids})
        return TriageResult(
            alert=alert.to_dict(),
            used_memory=use_memory,
            brief=brief,
            recalled=hits,
            insight=insight,
            cited_incidents=cited,
            llm_used=llm_used,
            memory_error=memory_error,
        )

    async def _write_brief(self, alert: Alert, memory_block: str, hits: list[MemoryHit]) -> tuple[str, bool]:
        if self.llm is not None:
            user = (
                f"ALERT {alert.id} [{alert.severity.upper()}] service={alert.service}\n{alert.title}\n\n"
                f"{alert.details}\n\nTEAM MEMORY:\n{memory_block}"
            )
            try:
                return await self.llm.complete(SYSTEM_PROMPT, user), True
            except LLMError as exc:
                log.warning("LLM unavailable, using template brief: %s", exc)
        return template_brief(alert, hits), False

    async def record_resolution(self, incident: Incident) -> None:
        await self.memory.retain(
            incident.to_memory_text(),
            tags=incident_tags(incident.service, incident.severity),
            context=f"Resolved production incident post-mortem for {incident.service}",
            document_id=incident.id,
            timestamp=incident.timestamp,
            metadata={"incident_id": incident.id, "service": incident.service, "severity": incident.severity},
        )

    async def record_feedback(self, alert: Alert, *, helpful: bool, notes: str) -> None:
        verdict = "HELPFUL - the suggested steps worked" if helpful else "NOT HELPFUL - the suggestion was wrong or wasted time"
        await self.memory.retain(
            f"Responder feedback on the RecallOps triage brief for alert {alert.id} on {alert.service} "
            f"('{alert.title}'): {verdict}. Responder notes: {notes.strip() or 'none'}",
            tags=incident_tags(alert.service, alert.severity, kind="feedback"),
            context="On-call responder feedback on copilot suggestions",
            document_id=f"feedback-{alert.id}-{int(datetime.now(timezone.utc).timestamp())}",
            timestamp=datetime.now(timezone.utc),
        )


def _field(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}:\s*(.+)", text)
    return match.group(1).strip() if match else None


def _incident_id(hit: MemoryHit) -> str | None:
    match = ID_PATTERN.search(hit.text) or ID_PATTERN.search(hit.document_id or "")
    return match.group(0) if match else None


def template_brief(alert: Alert, hits: list[MemoryHit]) -> str:
    """Deterministic brief used when no LLM is configured or the LLM call fails."""
    if not hits:
        return (
            "### Likely cause\nThis looks new to the team: no similar incident on record.\n\n"
            "### Do this now\n"
            "1. Check what changed in the last hour: `argocd app history " + alert.service + "` and recent config/infra changes\n"
            "2. If a deploy lines up with the start time, roll back first and debug second\n"
            "3. Check saturation: pod restarts, CPU/memory, connection pools, queue depth\n"
            "4. Open an incident channel and page the service owner\n\n"
            "### Avoid\nRestarting everything before capturing logs and metrics.\n\n"
            "### Similar past incidents\nNone on record\n\n"
            "### Confidence\nLow: no team history to draw on."
        )
    structured = [h for h in hits if "Root cause:" in h.text]
    if structured:
        top = structured[0].text
        cause = _field(top, "Root cause") or "See the similar incident below."
        steps = [s.strip() for s in re.split(r"\(\d+\)\s*", _field(top, "Remediation that worked") or "") if s.strip()]
        avoid = _field(top, "Tried and did NOT help (avoid wasting time on these)") or "Nothing recorded."
    else:
        cause = "Team memory holds related facts; the most relevant are listed as steps below."
        steps = [h.text for h in hits[:4]]
        avoid = "Anything the recalled facts mark as not helping before."
    fix = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "Follow the remediation from the similar incident."
    ids = list(dict.fromkeys(i for i in map(_incident_id, hits) if i))
    similar = "\n".join(f"- {i}" for i in ids[:4]) or "None on record"
    return (
        f"### Likely cause\n{cause}\n\n### Do this now\n{fix}\n\n### Avoid\n{avoid}\n\n"
        f"### Similar past incidents\n{similar}\n\n### Confidence\nMedium: matched on team memory without LLM synthesis."
    )


async def seed_history(agent: RecallOpsAgent, incidents: list[Incident]) -> int:
    await agent.memory.ensure_bank()
    for incident in incidents:
        await agent.record_resolution(incident)
    return len(incidents)
