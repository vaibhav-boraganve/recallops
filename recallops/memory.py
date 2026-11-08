"""Memory layer for RecallOps.

Hindsight is the primary backend: incidents, resolutions, and responder feedback
are retained into a single memory bank for the SRE team, recalled when a new alert
fires, and reflected on to surface cross-incident patterns.

`LocalMemory` is a keyword-matching stand-in used by the test suite and for
running the UI offline. It has no reflection and much weaker recall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import Settings

log = logging.getLogger(__name__)

BANK_MISSION = (
    "You are the long-term memory of Acme Pay's SRE on-call team. Remember every "
    "production incident: the alert and symptoms, affected services, root cause, the "
    "exact remediation steps that worked, steps that were tried and did NOT work, "
    "time to resolve, and responder feedback on the copilot's suggestions."
)

REFLECT_MISSION = (
    "Answer as a senior SRE who has lived through every past incident. Be concrete: "
    "name past incident IDs, root causes, commands and fixes. Call out recurring "
    "patterns and approaches that wasted time before."
)


@dataclass
class MemoryHit:
    text: str
    type: str = "world"
    tags: list[str] = field(default_factory=list)
    occurred_at: str | None = None
    document_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class IncidentMemory(Protocol):
    name: str

    async def ensure_bank(self) -> None: ...

    async def retain(
        self,
        content: str,
        *,
        tags: list[str],
        context: str,
        document_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def recall(self, query: str, *, tags: list[str] | None = None, limit: int = 8) -> list[MemoryHit]: ...

    async def reflect(self, query: str, *, tags: list[str] | None = None) -> str | None: ...

    async def close(self) -> None: ...


class MemoryUnavailableError(RuntimeError):
    pass


class HindsightMemory:
    name = "hindsight"

    def __init__(self, base_url: str, bank_id: str, api_key: str | None = None) -> None:
        from hindsight_client import Hindsight

        self.bank_id = bank_id
        self.base_url = base_url
        self._client = Hindsight(base_url=base_url, api_key=api_key, timeout=120.0)

    async def ensure_bank(self) -> None:
        try:
            await self._client.acreate_bank(
                self.bank_id,
                name="Acme Pay SRE incident memory",
                mission=BANK_MISSION,
                reflect_mission=REFLECT_MISSION,
                enable_observations=True,
            )
        except Exception as exc:  # bank may already exist; anything else surfaces on first retain/recall
            log.debug("create_bank(%s) returned: %s", self.bank_id, exc)

    async def retain(
        self,
        content: str,
        *,
        tags: list[str],
        context: str,
        document_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        try:
            await self._client.aretain(
                self.bank_id,
                content,
                context=context,
                tags=tags,
                document_id=document_id,
                timestamp=timestamp,
                metadata=metadata,
            )
        except Exception as exc:
            raise MemoryUnavailableError(f"Hindsight retain failed: {exc}") from exc

    async def recall(self, query: str, *, tags: list[str] | None = None, limit: int = 8) -> list[MemoryHit]:
        try:
            response = await self._client.arecall(
                self.bank_id,
                query,
                budget="mid",
                max_tokens=4096,
                tags=tags or None,
                tags_match="any",
            )
        except Exception as exc:
            raise MemoryUnavailableError(f"Hindsight recall failed: {exc}") from exc
        hits = []
        for result in (response.results or [])[:limit]:
            occurred = result.occurred_start or result.mentioned_at
            hits.append(
                MemoryHit(
                    text=result.text,
                    type=result.type or "world",
                    tags=list(result.tags or []),
                    occurred_at=occurred.isoformat() if isinstance(occurred, datetime) else occurred,
                    document_id=result.document_id,
                )
            )
        return hits

    async def reflect(self, query: str, *, tags: list[str] | None = None) -> str | None:
        try:
            response = await self._client.areflect(self.bank_id, query, budget="low", tags=tags or None)
        except Exception as exc:
            log.warning("Hindsight reflect failed, continuing without insight: %s", exc)
            return None
        return (response.text or "").strip() or None

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "was", "were", "that", "this", "from", "after", "into",
    "are", "has", "had", "but", "not", "all", "via", "per", "its", "our", "out", "then",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


class LocalMemory:
    """JSON-file memory with keyword-overlap recall. Offline/test fallback only."""

    name = "local"
    min_score = 0.15

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._items: list[dict] = []
        if path.exists():
            try:
                self._items = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("Ignoring corrupt local memory file %s", path)

    async def ensure_bank(self) -> None:
        return None

    async def retain(
        self,
        content: str,
        *,
        tags: list[str],
        context: str,
        document_id: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        async with self._lock:
            self._items = [i for i in self._items if not document_id or i.get("document_id") != document_id]
            self._items.append(
                {
                    "text": content,
                    "tags": tags,
                    "context": context,
                    "document_id": document_id,
                    "occurred_at": (timestamp or datetime.now(timezone.utc)).isoformat(),
                    "metadata": metadata or {},
                }
            )
            self.path.write_text(json.dumps(self._items, indent=2))

    async def recall(self, query: str, *, tags: list[str] | None = None, limit: int = 8) -> list[MemoryHit]:
        docs = [(item, _tokens(item["text"])) for item in self._items]
        doc_freq: dict[str, int] = {}
        for _, tokens in docs:
            for token in tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        def idf(token: str) -> float:
            df = doc_freq.get(token, 0)
            return math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))

        q = _tokens(query)
        total = sum(idf(t) for t in q) or 1.0
        scored = []
        for item, tokens in docs:
            if tags and not set(tags) & set(item["tags"]):
                continue
            score = sum(idf(t) for t in q & tokens) / total
            if score >= self.min_score:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            MemoryHit(
                text=item["text"],
                type="experience" if "kind:feedback" in item["tags"] else "world",
                tags=item["tags"],
                occurred_at=item["occurred_at"],
                document_id=item["document_id"],
            )
            for _, item in scored[:limit]
        ]

    async def reflect(self, query: str, *, tags: list[str] | None = None) -> str | None:
        return None

    async def close(self) -> None:
        return None

    async def clear(self) -> None:
        async with self._lock:
            self._items = []
            if self.path.exists():
                self.path.unlink()


def build_memory(settings: Settings) -> IncidentMemory:
    if settings.memory_backend == "local":
        return LocalMemory(settings.local_memory_path)
    return HindsightMemory(settings.hindsight_url, settings.bank_id, settings.hindsight_api_key)
