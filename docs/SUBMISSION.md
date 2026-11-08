# RecallOps submission kit

## Submission checklist

- [ ] Push this repo to GitHub (public) with the README
- [ ] Record the demo video (script below, under 3 minutes)
- [ ] Live demo: Hindsight running and seeded, dashboard open, Band room open with RecallOps online
- [ ] Each team member: article, social post, and video per the official content guide
- [ ] "How Hindsight is used": README section *How Hindsight memory is used*

## Pre-demo setup (5 minutes before)

```bash
# use a fresh bank so the "never seen before" moment is real
export HINDSIGHT_BANK_ID=recallops-demo-$(date +%H%M)
uv run python scripts/seed_memory.py
uv run uvicorn recallops.server:app --port 8000 &
uv run python -m recallops.band_agent &
```

Open the dashboard, the Hindsight Cloud UI (https://ui.hindsight.vectorize.io), and the Band room `#inc-war-room`. `.env` must already contain `GROQ_API_KEY`, `HINDSIGHT_API_KEY`, and `HINDSIGHT_URL=https://api.hindsight.vectorize.io`.

## Demo video script (about 2.5 minutes)

**0:00 Problem (20s).** "It's 3am. Payments are failing. The on-call engineer has never seen this, but the team has. That knowledge is buried in old post-mortems. Generic AI can't help, because it doesn't know *your* systems or *your* history."

**0:20 Act 1, memory vs no memory (45s).** Click **ALERT-7781**. Left: the generic LLM says "check recent deploys, consider rollback". Right: RecallOps recognises INC-2026-0412, gives the exact `argocd app rollback` and the `pg_terminate_backend` SQL, and warns *"don't raise the pool size, that failed twice before."* Expand **What RecallOps remembered** to show the recalled Hindsight memories and the reflect insight.

**1:05 Act 2, something new (25s).** Click **ALERT-7815** (cert expired). RecallOps honestly says this looks new. "Our engineer fixes it: a WAF rule was blocking Let's Encrypt." Click **prefill**, then **Retain in memory**. Optionally show the new facts in the Hindsight UI.

**1:30 Act 3, it learned (30s).** "One week later, a different service." Click **ALERT-7840**. RecallOps now cites INC-2026-1108 and goes straight to the WAF rule and `cmctl renew`. "Same agent, same model. The only difference is memory."

**2:00 Band (25s).** Switch to the Band room. Paste the ledger lag alert with `@RecallOps`; it replies with the brief citing INC-2026-0303. Then type "that worked, thanks" and it records the feedback. "It lives where the team already works, and everything it learns here is shared with the dashboard."

**2:25 Close (5s).** "RecallOps: your team's incident memory, on call 24/7."

## Article draft (Dev.to, Hashnode, or Medium)

**Title:** I built an on-call copilot that remembers every incident, and it got smarter in 3 alerts

1. **The 3am problem.** Mean time to resolve is dominated by rediscovery. The fix for most incidents already exists in someone's head or a post-mortem doc.
2. **Why stateless LLMs fail at this.** Show the generic brief for ALERT-7781. It's correct but useless.
3. **Adding memory with Hindsight.** Explain the bank mission, `retain` with `document_id`, timestamps, and tags, then `recall` and `reflect`, with code snippets from `recallops/agent.py`.
4. **Watching it learn.** The three-act demo with screenshots: generic, then "this looks new", then recalled.
5. **Living in Band.** The LangGraph adapter with `additional_tools`, so the agent is a participant in the incident room rather than another tab.
6. **Guardrails.** Citation verification (only recalled incident IDs are reported), graceful degradation when Hindsight or the LLM are down, and input validation.
7. **What's next.** PagerDuty webhooks, post-mortem auto-ingest, per-team banks.

## Social post draft (LinkedIn / X)

> At 3am, the hardest part of an outage is not the fix, it's remembering that your team already fixed this exact thing 6 months ago.
>
> For #HackwithHyderabad 3.0 I built **RecallOps**, an on-call copilot with long-term memory powered by @Hindsight (Vectorize) that also lives in a @Band incident room.
>
> 🔴 Alert fires → RecallOps recalls similar past incidents, the fix that worked, and what wasted time before
> 🧠 Engineer resolves something new → it's retained
> ✅ A week later, same failure on a different service → RecallOps already knows
>
> Same model. The only difference is memory.
>
> Demo + code: <repo link>
> #AIAgents #SRE #DevOps #Hindsight #LLM
