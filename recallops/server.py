from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import RecallOpsAgent, seed_history
from .config import ROOT_DIR, Settings, load_settings
from .data import DEMO_ALERTS, DEMO_NEW_RESOLUTION, HISTORICAL_INCIDENTS, Alert, Incident
from .llm import build_llm
from .memory import IncidentMemory, LocalMemory, MemoryUnavailableError, build_memory

log = logging.getLogger(__name__)
STATIC_DIR = ROOT_DIR / "static"


@dataclass
class AppState:
    settings: Settings
    memory: IncidentMemory
    agent: RecallOpsAgent


class AlertIn(BaseModel):
    id: str = Field(default_factory=lambda: f"ALERT-{int(datetime.now(timezone.utc).timestamp()) % 100000}")
    title: str = Field(min_length=5, max_length=300)
    service: str = Field(min_length=2, max_length=60)
    severity: str = Field(default="sev2", pattern=r"^sev[1-4]$")
    details: str = Field(default="", max_length=6000)

    def to_alert(self) -> Alert:
        return Alert(**self.model_dump())


class TriageIn(BaseModel):
    alert: AlertIn
    use_memory: bool = True


class ResolutionIn(BaseModel):
    id: str = Field(pattern=r"^INC-\d{4}-\d{4}$")
    title: str = Field(min_length=5, max_length=300)
    service: str = Field(min_length=2, max_length=60)
    severity: str = Field(default="sev2", pattern=r"^sev[1-4]$")
    responder: str = Field(default="on-call", max_length=80)
    symptoms: str = Field(min_length=5, max_length=4000)
    root_cause: str = Field(min_length=5, max_length=4000)
    fix_steps: list[str] = Field(min_length=1, max_length=12)
    did_not_work: list[str] = Field(default_factory=list, max_length=12)
    minutes_to_resolve: int = Field(default=0, ge=0, le=100000)
    follow_ups: list[str] = Field(default_factory=list, max_length=12)

    def to_incident(self) -> Incident:
        data = self.model_dump()
        data["fix_steps"] = [s.strip() for s in self.fix_steps if s.strip()]
        data["did_not_work"] = [s.strip() for s in self.did_not_work if s.strip()]
        data["occurred_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return Incident(**data)


class FeedbackIn(BaseModel):
    alert: AlertIn
    helpful: bool
    notes: str = Field(default="", max_length=2000)


def create_app(settings: Settings | None = None, memory: IncidentMemory | None = None, llm=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or load_settings()
        mem = memory or build_memory(cfg)
        try:
            await mem.ensure_bank()
        except Exception as exc:
            log.warning("Could not ensure memory bank on startup: %s", exc)
        agent = RecallOpsAgent(mem, llm if llm is not None else build_llm(cfg))
        app.state.rec = AppState(cfg, mem, agent)
        yield
        await mem.close()

    app = FastAPI(title="RecallOps", version="0.1.0", lifespan=lifespan)

    def state() -> AppState:
        return app.state.rec

    def memory_failure(exc: MemoryUnavailableError) -> HTTPException:
        return HTTPException(status_code=503, detail=f"Memory backend unavailable: {exc}")

    @app.get("/api/status")
    async def status():
        s = state()
        return {
            "memory_backend": s.memory.name,
            "bank_id": s.settings.bank_id,
            "hindsight_url": s.settings.hindsight_url if s.memory.name == "hindsight" else None,
            "llm_model": s.settings.llm_model if s.agent.llm else None,
        }

    @app.get("/api/alerts")
    async def alerts():
        return [a.to_dict() for a in DEMO_ALERTS]

    @app.get("/api/history")
    async def history():
        return [i.to_dict() for i in HISTORICAL_INCIDENTS]

    @app.post("/api/triage")
    async def triage(body: TriageIn):
        result = await state().agent.triage(body.alert.to_alert(), use_memory=body.use_memory)
        return result.to_dict()

    @app.post("/api/compare")
    async def compare(body: AlertIn):
        alert = body.to_alert()
        agent = state().agent
        without, with_memory = await asyncio.gather(
            agent.triage(alert, use_memory=False), agent.triage(alert, use_memory=True)
        )
        return {"without_memory": without.to_dict(), "with_memory": with_memory.to_dict()}

    @app.post("/api/resolutions")
    async def record_resolution(body: ResolutionIn):
        try:
            await state().agent.record_resolution(body.to_incident())
        except MemoryUnavailableError as exc:
            raise memory_failure(exc) from exc
        return {"retained": body.id}

    @app.post("/api/feedback")
    async def feedback(body: FeedbackIn):
        try:
            await state().agent.record_feedback(body.alert.to_alert(), helpful=body.helpful, notes=body.notes)
        except MemoryUnavailableError as exc:
            raise memory_failure(exc) from exc
        return {"retained": True}

    @app.get("/api/memories")
    async def memories(q: str):
        if not q.strip():
            raise HTTPException(status_code=422, detail="q must not be empty")
        try:
            hits = await state().memory.recall(q, limit=15)
        except MemoryUnavailableError as exc:
            raise memory_failure(exc) from exc
        return [h.to_dict() for h in hits]

    @app.post("/api/demo/seed")
    async def demo_seed():
        try:
            count = await seed_history(state().agent, HISTORICAL_INCIDENTS)
        except MemoryUnavailableError as exc:
            raise memory_failure(exc) from exc
        return {"retained": count}

    @app.get("/api/demo/new-resolution")
    async def demo_new_resolution():
        return DEMO_NEW_RESOLUTION.to_dict()

    @app.post("/api/demo/reset")
    async def demo_reset():
        mem = state().memory
        if not isinstance(mem, LocalMemory):
            raise HTTPException(
                status_code=400,
                detail="Reset is only supported for the local backend. For Hindsight, change HINDSIGHT_BANK_ID or delete the bank in the Hindsight UI.",
            )
        await mem.clear()
        return {"cleared": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
