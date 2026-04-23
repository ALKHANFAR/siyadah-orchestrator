# Pattern Library Seed — extracted from curated cloud templates

> Step A of Wave-0 completion (revised — see §8 for the supersession note).
> Primary source: **420 templates** from the Activepieces cloud library,
> fetched via `GET /api/v1/templates/{id}` (mirrored on our Railway AP via
> `AP_TEMPLATES_SOURCE_URL=https://cloud.activepieces.com/api/v1/flow-templates`).
> Secondary source (reference only): 147 live flows from the production
> project `ou4jOTA4KMnDrzOVsKWvd` — see §8 why they were demoted.
> Method: pure structural hashing — no LLM.
> Output: `data/patterns-seed.json`.

---

## 1. Headline

The Pattern Library is built from **420 curated, production-quality templates**,
not the 147 polluted live flows. The difference is night-and-day:

| Dimension | 147 live flows | 420 cloud templates |
|---|---:|---:|
| Unique structural signatures | 55 | **358** |
| Recurring patterns (≥ 2 instances) | 17 | **57** |
| Templates with ROUTER (branching) | 0 | **181 (43 %)** |
| Templates with LOOP | 0 | **114 (27 %)** |
| Templates with CODE | 0 | **77 (18 %)** |
| Distinct triggers in recurring patterns | webhook only | schedule, webhook, typeform, slack, forms, gmail, github, trello, woocommerce, google-contacts, google-drive, pipedrive, fillout-forms, google-forms, google-calendar |
| Unique pieces referenced | 33 | **114** |

The cloud library is the **source of truth** for PBWG. The 147 live flows
show what *our* customers asked for so far (narrow: Gmail + Sheets +
WhatsApp); the 420 templates show what *any* business could ask for
(CRM, marketing automation, HR, finance, operations, IT).

---

## 2. The Golden Library — 57 recurring patterns

Top 30 by instance count (full list in `data/patterns-seed.json`):

| Rank | Signature | × | Trigger | Steps | R/L/C | Primary pieces | Canonical name |
|---:|---|---:|---|---:|:---:|---|---|
| 1 | `a13c52e0dcfe` | 4 | schedule | 7 | R/L/C | date-helper, gmail, google-sheets | Monitor Inventory for Low Stock and Expiring Items |
| 2 | `e91b24e21962` | 3 | typeform | 1 | · | odoo | Create Odoo CRM Leads from Typeform Responses |
| 3 | `76f1079e1ae2` | 3 | slack | 1 | · | ai | Answer Employee Policy Questions Instantly |
| 4 | `c7cae0f1d9dd` | 3 | forms | 8 | R/L/C | google-sheets, http | Crawl a Website Sitemap and Extract All URLs |
| 5 | `58f20f2befce` | 2 | schedule | 9 | R/L | gmail, google-docs, google-drive | Auto-Generate and Auto-Fill Business Documents |
| 6 | `6dc4f0d070ee` | 2 | webhook | 2 | · | openai, webhook | Convert Audio To Text |
| 7 | `14b29ca70616` | 2 | schedule | 4 | · /·/C | data-summarizer, hubspot, slack | Weekly HubSpot Lead Report to Slack |
| 8 | `d5dbf52ef0fd` | 2 | gmail | 2 | · | agent, gmail | Email Autoresponder |
| 9 | `3b2b65a16050` | 2 | github | 16 | R | discord, store | Route GitHub Pull Requests to Discord |
| 10 | `7167f8f55437` | 2 | webhook | 12 | R | agent, gmail, google-sheets, todos | Review Tally Applications |
| 11 | `ddae5f993388` | 2 | schedule | 11 | R/L | date-helper, google-sheets, notion | Daily Task Deadline Reminders |
| 12 | `114d63d7e5d4` | 2 | trello | 1 | · | slack | Send Slack Messages for New Trello Cards |
| 13 | `76afdb171fdf` | 2 | woocommerce | 1 | · | telegram-bot | Send Telegram Notification for New WooCommerce Orders |
| 14 | `2776ef6cd8b4` | 2 | schedule | 6 | · /L/C | google-sheets, youtube | Gather Complete YouTube Channel Data |
| 15 | `529a57f663ba` | 2 | google-contacts | 7 | R | activecampaign, data-mapper | Sync Google Contacts to ActiveCampaign |
| 16 | `d8f7acf17c47` | 2 | forms | 5 | R | ai, gmail, salesforce | Salesforce Lead Capture with GPT-4 |
| 17 | `833a1043258d` | 2 | google-forms | 1 | · | trello | Create Trello cards from Google Forms |
| 18 | `fddb3aa699fa` | 2 | forms | 5 | R | gmail, salesforce, text-ai | Salesforce Lead Capture with GPT-5 |
| 19 | `04b88b600f2b` | 2 | slack | 12 | R/L | exa, gmail, serp-api, tables | Lead Generation |
| 20 | `42531cafb453` | 2 | schedule | 4 | · | date-helper, google-calendar | Automatic Scraping of Company Information |
| 21 | `08b071f74d43` | 2 | typeform | 1 | · | zendesk | Create Zendesk tickets from Typeform |
| 22 | `1cf873a75fd0` | 2 | forms | 5 | · | apify, csv, google-sheets | Extract Facebook Group Posts |
| 23 | `d698f2513bbd` | 2 | schedule | 3 | · /·/C | jira-cloud, notion | Weekly overview of JIRA |
| 24 | `9105265f199b` | 2 | google-drive | 2 | · | agent, openai | Create SEO Titles for Shorts/Reels |
| 25 | `aaf8e3a291ef` | 2 | pipedrive | 2 | · | clickup, pipedrive | Pipedrive to ClickUp |
| 26 | `1fdc3d72f083` | 2 | forms | 7 | · /·/C | forms, google-drive, http | Download TikTok Videos without Watermark |
| 27 | `ebd38743f640` | 2 | schedule | 9 | R/L | date-helper, firecrawl, gmail | Gather Latest Industry News |
| 28 | `e2adcf6e82fb` | 2 | trello | 4 | R | date-helper, google-calendar | Create Google Calendar events from Trello |
| 29 | `bef6ed275df0` | 2 | fillout-forms | 4 | R | gmail, google-calendar, zerobounce | Auto-Validate Email & Send Event Invites |
| 30 | `5ea334676ef1` | 2 | google-drive | 10 | R/L/C | file-helper, google-drive, openai, pinecone | Index Documents from Google Drive to Pinecone |

Singletons (301 templates with unique structure) are kept in
`data/patterns-seed.json` → `singletons_sample[]` (capped at 50 for file
size) as the **promotion watchlist**: any signature that gains a second
instance on a future cloud sync is auto-promoted to a pattern.

---

## 3. Flow-control footprint

| Construct | Templates using it | % | What it proves |
|---|---:|---:|---|
| ROUTER (BRANCH) | **181** | 43 % | Multi-path logic is table stakes, not an edge case. |
| LOOP_ON_ITEMS | **114** | 27 % | Lists/arrays are common in real work. |
| CODE | **77** | 18 % | JS code steps are an expected escape hatch. |

Our orchestrator today supports ROUTER + LOOP + CODE at build time
(`main.py:1127–1149`, `1087–1125`, `1068–1085`) — so the primitives exist.
**The PBWG Parameter Binder must handle all three**, not just linear flows.

---

## 4. Trigger diversity (the blind spot of our current stack)

Cloud templates' trigger distribution (top 15):

| Trigger piece | Templates |
|---|---:|
| schedule | 115 |
| (various webhooks under `webhook` piece) | ≈ 40 |
| typeform | 9 |
| slack | 7 |
| forms (AP built-in) | 20+ |
| gmail | 6 |
| github | 5 |
| trello | 5 |
| woocommerce | 4 |
| google-drive | 4 |
| google-forms | 3 |
| pipedrive | 3 |
| fillout-forms | 3 |
| google-calendar | 2 |
| google-contacts | 2 |

Our orchestrator today exposes only `webhook` and `schedule` helpers
(`main.py:827-846`). **Every other trigger above requires a caller to
pass a raw piece + triggerName** — the engine can do it, but the
orchestrator's high-level API does not.

### Required follow-up (Wave 3)

- Expose `piece_trigger` as a first-class template argument.
- Pass `triggerName` + `input` (with `{{connections[...]}}` for auth) through to `_build_step_from_spec`.
- Document the top 10 non-webhook triggers (typeform, forms, slack, gmail, github, trello, woocommerce, google-drive, google-contacts, pipedrive) with example input shapes.

---

## 5. Categories — the sector vocabulary

Cloud templates self-categorise. Top 10:

| Category | Templates |
|---|---:|
| Marketing | 62 |
| Operations | 57 |
| Sales | 49 |
| ✨ Everyday | 29 |
| HR | 25 |
| Customer Service | 24 |
| Finance | 23 |
| IT | 21 |
| Product | 14 |
| Featured | 13 |

These 10 categories map closely to the **sector playbooks** described in
the original briefing. PBWG's category field lets us present
sector-appropriate patterns without custom classification.

---

## 6. Piece vocabulary (top 30)

| Piece | Templates |
|---|---:|
| google-sheets | 192 |
| ai (generic Claude/OpenAI wrapper) | 155 |
| gmail | 153 |
| schedule | 115 |
| slack | 111 |
| text-ai | 97 |
| date-helper | 80 |
| google-drive | 63 |
| forms | 61 |
| store (AP key-value store) | 58 |
| utility-ai | 54 |
| http | 51 |
| data-mapper | 46 |
| hubspot | 39 |
| text-helper | 32 |
| tables (AP native tables) | 32 |
| notion | 31 |
| openai | 30 |
| telegram-bot | 29 |
| salesforce | 26 |
| pipedrive | 25 |
| firecrawl | 22 |
| google-calendar | 20 |
| perplexity-ai | 19 |
| delay | 18 |
| …and 89 more, total 114 distinct | |

Against our current 3 active connections (Gmail, Sheets, Drive) in the
production project, **most of these pieces have no connection** — any
template using them will fail `guard_connections` (`main.py:416`)
unless the tenant connects the required piece first. This is the
expected friction; PBWG must surface "you need to connect X" clearly.

---

## 7. What this means for PBWG

```
user intent ───────┐
                   ▼
          intent classifier
          (rule-based, no LLM)
                   ▼
     category match against patterns-seed.json.patterns[].categories
                   ▼
     trigger-piece match (from patterns[].trigger.piece)
                   ▼
     shortlist 3-5 patterns, ranked by instance_count
                   ▼
     check guard_connections — if any pattern needs missing pieces,
     either (a) present the next-best candidate, or
     (b) prompt user to connect the required piece
                   ▼
     fetch full JSON for the chosen pattern's first instance_template_id
                   ▼
     parameter_binder.py — rewrite inputs for the tenant (preserve {{trigger.*}})
                   ▼
     golden_build() deploys
```

**Zero LLM JSON generation on the hot path.** The LLM only maps natural-
language intent to a category + trigger-piece pair. Everything else is
lookup and mechanical rewrite.

---

## 8. Why we moved off the 147 live flows (supersession note)

The first iteration of this report (commit `f737…`) extracted patterns
from the 147 live flows in the production project. That yielded 17
patterns covering 74 % of flows. Operator feedback:

> "الحالي 147 ليس أفضل شي — هذه الـ flow جاهزة عندك في المنصة وواضحة"

Confirmed correct. The 147 flows are **polluted**:

- 29 are developer test series (`fuzzy test`, `Strict Guard Test`,
  `Breaking Point Test v5.8.0`, `complex test`, `Test Auto Resolve`).
- 53 are duplicates (see `reports/empirical-findings.md §2` — 49 of
  the 147 are structural duplicates of 9 base shapes).
- All 147 trigger on webhook — no diversity — because the orchestrator
  never offered anything else.
- Zero use ROUTER / LOOP / CODE — the customers never got to the point
  of needing them before duplicate-creation fatigue set in.

The 420 cloud templates are **clean, diverse, production-authored by
the AP team**. They are the right seed.

The 147-flow extraction is preserved in `data/patterns-seed.legacy-147flows.json`
(optional; not committed) as reference for usage-frequency weighting
in a future iteration.

---

## 9. Reproducibility

```bash
# End-to-end rebuild
AP=https://activepieces-production-2499.up.railway.app
TOKEN=$(curl -s -X POST "$AP/api/v1/authentication/sign-in" \
          -H "Content-Type: application/json" \
          -d "{\"email\":\"$AP_EMAIL\",\"password\":\"$AP_PASSWORD\"}" \
        | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 1. list index
curl -s "$AP/api/v1/templates?limit=1000" -H "Authorization: Bearer $TOKEN" \
     > /tmp/templates.json

# 2. fetch each template in parallel (16 workers)
mkdir -p /tmp/templates-full
python3 -c "
import json, concurrent.futures, urllib.request
idx = json.load(open('/tmp/templates.json'))
ids = [t['id'] for t in (idx.get('data') or idx)]
TOKEN = '$TOKEN'
def fetch(tid):
    req = urllib.request.Request(f'$AP/api/v1/templates/{tid}',
        headers={'Authorization': f'Bearer {TOKEN}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    open(f'/tmp/templates-full/{tid}.json','wb').write(data)
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as p:
    list(p.map(fetch, ids))
"

# 3. extract patterns (script to be added in scripts/extract-patterns.py in Wave 1)
python3 scripts/extract-patterns.py
```

---

## 10. Next steps

1. Commit `data/patterns-seed.json` + this report.
2. Add `scripts/extract-patterns.py` as a reproducible generator (Wave 1).
3. Wire PBWG (Wave 3) to consume `data/patterns-seed.json`.
4. Add a nightly cron that re-syncs cloud templates and regenerates the seed (Wave 7).

*End of Step A revised.*
