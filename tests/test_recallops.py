from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recallops.agent import RecallOpsAgent, seed_history  # noqa: E402
from recallops.config import load_settings  # noqa: E402
from recallops.data import DEMO_ALERTS, DEMO_NEW_RESOLUTION, HISTORICAL_INCIDENTS  # noqa: E402
from recallops.llm import LLMError  # noqa: E402
from recallops.memory import LocalMemory, MemoryHit, MemoryUnavailableError  # noqa: E402
from recallops.server import create_app  # noqa: E402

ALERTS = {a.id: a for a in DEMO_ALERTS}


class EchoLLM:
    """Echoes the incident IDs it was given so tests can check grounding."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        self.prompts.append(user)
        ids = sorted(set(re.findall(r"INC-\d{4}-\d{4}", user)))
        return "### Similar past incidents\n" + ("\n".join(f"- {i}" for i in ids) or "None on record") + "\n- INC-1999-0001"


class BrokenLLM:
    async def complete(self, *args, **kwargs) -> str:
        raise LLMError("rate limited")


class DownMemory(LocalMemory):
    async def recall(self, *args, **kwargs):
        raise MemoryUnavailableError("connection refused")


@pytest.fixture
def memory(tmp_path):
    return LocalMemory(tmp_path / "mem.json")


def run(coro):
    return asyncio.run(coro)


def test_triage_without_memory_is_generic(memory):
    agent = RecallOpsAgent(memory, EchoLLM())
    run(seed_history(agent, HISTORICAL_INCIDENTS))
    result = run(agent.triage(ALERTS["ALERT-7781"], use_memory=False))
    assert result.recalled == []
    assert result.cited_incidents == []


def test_triage_with_memory_recalls_matching_incident(memory):
    agent = RecallOpsAgent(memory, EchoLLM())
    run(seed_history(agent, HISTORICAL_INCIDENTS))
    result = run(agent.triage(ALERTS["ALERT-7781"]))
    assert "INC-2026-0412" in result.cited_incidents
    assert result.llm_used


def test_hallucinated_incident_ids_are_not_reported_as_cited(memory):
    agent = RecallOpsAgent(memory, EchoLLM())
    run(seed_history(agent, HISTORICAL_INCIDENTS))
    result = run(agent.triage(ALERTS["ALERT-7802"]))
    assert "INC-1999-0001" in result.brief
    assert "INC-1999-0001" not in result.cited_incidents


def test_agent_learns_new_failure_mode(memory):
    agent = RecallOpsAgent(memory, EchoLLM())
    run(seed_history(agent, HISTORICAL_INCIDENTS))
    before = run(agent.triage(ALERTS["ALERT-7840"]))
    assert DEMO_NEW_RESOLUTION.id not in before.cited_incidents

    run(agent.record_resolution(DEMO_NEW_RESOLUTION))
    after = run(agent.triage(ALERTS["ALERT-7840"]))
    assert DEMO_NEW_RESOLUTION.id in after.cited_incidents


def test_re_recording_an_incident_replaces_it(memory):
    agent = RecallOpsAgent(memory, None)
    run(agent.record_resolution(DEMO_NEW_RESOLUTION))
    run(agent.record_resolution(replace(DEMO_NEW_RESOLUTION, root_cause="WAF blocked ACME challenge path")))
    hits = run(memory.recall("certificate expired acme challenge"))
    assert len([h for h in hits if DEMO_NEW_RESOLUTION.id in h.text]) == 1


def test_falls_back_to_template_when_llm_fails(memory):
    agent = RecallOpsAgent(memory, BrokenLLM())
    run(seed_history(agent, HISTORICAL_INCIDENTS))
    result = run(agent.triage(ALERTS["ALERT-7781"]))
    assert not result.llm_used
    assert "### Do this now" in result.brief
    assert result.cited_incidents


def test_template_brief_for_unknown_failure(memory):
    agent = RecallOpsAgent(memory, None)
    result = run(agent.triage(ALERTS["ALERT-7815"]))
    assert "looks new" in result.brief
    assert result.cited_incidents == []


def test_memory_outage_degrades_gracefully(tmp_path):
    agent = RecallOpsAgent(DownMemory(tmp_path / "m.json"), None)
    result = run(agent.triage(ALERTS["ALERT-7781"]))
    assert result.memory_error
    assert result.brief


class FactMemory(LocalMemory):
    """Mimics Hindsight: recalled facts are extracted sentences that only carry the ID as document_id."""

    async def recall(self, *args, **kwargs):
        return [MemoryHit(text="PgBouncer max_client_conn was raised to 1000 after pool exhaustion", document_id="INC-2026-0117")]


def test_cites_incidents_via_document_id(tmp_path):
    for llm in (EchoLLM(), None):
        result = run(RecallOpsAgent(FactMemory(tmp_path / "f.json"), llm).triage(ALERTS["ALERT-7781"]))
        assert result.cited_incidents == ["INC-2026-0117"]


def test_feedback_is_recalled(memory):
    agent = RecallOpsAgent(memory, None)
    run(agent.record_feedback(ALERTS["ALERT-7802"], helpful=False, notes="lag was a poison message on partition 3"))
    hits = run(memory.recall("ledger-service consumer lag poison message"))
    assert any("NOT HELPFUL" in h.text for h in hits)


def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "local")
    monkeypatch.setenv("LOCAL_MEMORY_PATH", str(tmp_path / "api.json"))
    app = create_app(settings=load_settings(), memory=LocalMemory(tmp_path / "api.json"), llm=EchoLLM())
    with TestClient(app) as client:
        assert client.get("/api/status").json()["memory_backend"] == "local"
        assert client.post("/api/demo/seed").json()["retained"] == len(HISTORICAL_INCIDENTS)

        alert = client.get("/api/alerts").json()[0]
        compare = client.post("/api/compare", json=alert).json()
        assert compare["without_memory"]["recalled"] == []
        assert "INC-2026-0412" in compare["with_memory"]["cited_incidents"]

        bad = client.post("/api/resolutions", json={"id": "nope", "title": "x"})
        assert bad.status_code == 422

        resolution = client.get("/api/demo/new-resolution").json()
        resolution.pop("occurred_at")
        assert client.post("/api/resolutions", json=resolution).status_code == 200
        hits = client.get("/api/memories", params={"q": "certificate expired cert-manager"}).json()
        assert any(DEMO_NEW_RESOLUTION.id in h["text"] for h in hits)

        assert client.post("/api/demo/reset").json() == {"cleared": True}
        assert client.get("/").status_code == 200
