# Siyadah OS — Master Execution Plan

> Crystallized after 10 refinement iterations.
> Written: 2026-04-22 · Branch: `claude/setup-siyadah-os-briefing-kboY9`
> Author: Claude (verified every number against `main.py`, `AGENTS.md`, WebFetch)

---

## 1. Context

The user owns three systems that must together become an **Autonomous Company OS**:

1. **Siyadah-6.5** (`github.com/ALKHANFAR/Siyadah-6.5`) — Next.js 16 BFF + Chat UI.
2. **siyadah-orchestrator** (this repo) — FastAPI build engine.
3. **Activepieces** (self-hosted) — 687-piece automation runtime.

Two planning briefings (§12 §13 of the original master briefing + the 40-scenario
catalogue + `CLAUDE.md v3`) described the target state. Direct inspection of
`main.py` revealed the briefings drift from reality — some numbers wrong, some
security claims exaggerated, some multi-tenancy claims untrue. This plan is
corrected against what the code actually does.

**Goal**: reach the 10 sovereign capabilities (C1–C10) below in 7 waves, without
fabrication, without skipping lower-layer integrity, without selling features
the runtime cannot actually deliver.

---

## 2. Verified Ground Truth (what I re-checked)

| Claim in briefing | Reality | Source |
|---|---|---|
| Orchestrator 24 endpoints | **38 endpoints** | `grep -cE "^@app\." main.py` |
| `except Exception` ×59 | **36** | `grep -c "except Exception" main.py` |
| AP has 661 pieces | **687** | `AGENTS.md:51` |
| Multi-tenancy fully enforced | **half-built**: schema has `project_id` FK, but auth does not verify caller owns it | `models.py:21-73` + `main.py:1870,…,4038` |
| CORS secure | `allow_origins=["*"], allow_credentials=True` | `main.py:1761` |
| API-key comparison secure | `if key != ORCH_API_KEY` (timing-leak) | `main.py:1772` |
| `agents.md` is a "no-hallucination contract" | **false** — it is just a README convention for AI agents | WebFetch |
| Universal 360° absorption exists | **only website** via Firecrawl; no GMaps, no IG, no competitors | `ingestion.py:1-80` |
| SSE tenant-safe | Session in Redis with TTL 3600, **no tenant binding** | `mcp_sse.py:27-60` |

---

## 3. Corrections applied to the briefing

1. **No fixed E1–E7 employee catalogue.** Replaced by **Dynamic Employee Spawner** — generates N employees per tenant with auto-chosen names/roles/prompts from DNA + sector + KB.
2. **Pricing ≠ four tiers.** One entry package ($25/mo) + Wallet-metered usage. `UsageMeter` becomes a first-class service.
3. **50-tool locked list ≠ real.** Toolkit is dynamic per tenant (Smart Pruning by sector + connected pieces).
4. **40 scenarios ≠ 40 implementations.** Extract 14 Pattern Primitives + 5 variables-that-change-everything + 10 failure-modes from them. Scenarios become E2E fixtures, not hard-coded features.
5. **"24 endpoints" → 38.** All counts in kb/docs updated.
6. **"661 pieces" → 687.**
7. **"Multi-tenancy is real" → half-built.** Treat tenant isolation as an architectural project, not a bug-fix.
8. **Waterfall Ceiling is strict.** A gate sits between every wave — no UI polish while lower layers leak.
9. **`agents.md` source-citation contract is fabricated.** The *principle* of writing a README for agents is adopted (see `AGENTS.md`), but no pretend "cite-source" methodology is cited.
10. **Wave 0 is blocking.** No code written anywhere until exported BRANCH + LOOP flows + AP staging token are provided; otherwise PBWG will hallucinate JSON again.

---

## 4. The 10 Sovereign Capabilities (what must exist)

| # | Capability | Summary |
|---|---|---|
| C1 | Universal Absorption 360° | URL → DNA across website + GMaps + IG + competitors + language + tone + currency + compliance |
| C2 | Dynamic Employee Spawner | Generates employees (count, names, roles, dedicated prompts) per tenant |
| C3 | PBWG Flow Engine | Builds flows that *actually run* — via import + Parameter Binder + dynamic values |
| C4 | Flow Awareness + Conflict Detection | Edits existing flows; detects overlapping triggers |
| C5 | Zero-Leak Silent Execution | No tool names, no JSON to user — results only |
| C6 | Continuous Re-absorption | Every message / website change → TenantBrain update |
| C7 | Real Multi-tenancy + Compliance Router + Data Residency | Verified tenant isolation; PDPL / GDPR / HIPAA / LGPD; region-aware DB |
| C8 | UsageMeter + Wallet Metering | Every LLM token, flow exec, MCP call, scrape counted live |
| C9 | Proactive Intelligence + Dosing | day1 → day3 → week1 → … tone and depth; proactive alerts |
| C10 | SafetyLayer + Crisis Routing | Crisis detection → immediate human + AI pause; per-country hotlines |

---

See this file on branch for the full 7-wave execution matrix, acceptance gates, and verification plan. The remaining sections (5–11) are in the branch.

*End of plan stub — full plan in branch.*
