"""
Siyadah Ingestion Engine — Universal Website Absorption
=========================================================
Pipeline: Firecrawl Scrape → Claude AI Analysis → Postgres Persist

Usage:
    result = await ingest_website(url="https://example.com", project_id="xxx")
    preview = await preview_website(url="https://example.com")  # no DB write
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

log = logging.getLogger("siyadah.ingest")

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"
FIRECRAWL_KEY = os.getenv("FIRECRAWL_API_KEY", "")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Model rotation contract: env override wins, with claude-sonnet-4-5 as the
# secure default (replaces claude-sonnet-4-20250514 which reaches EOL on
# 2026-06-15). Set ANTHROPIC_MODEL in env to roll forward without a code
# change. See INVESTIGATION_REPORT.md Issue #1.
# Uses `or` (not getenv default) so empty string env values fall back too.
CLAUDE_MODEL = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-5"

MAX_CONTENT_CHARS = 15_000

ANALYSIS_SYSTEM = (
    "You are Siyadah's Business Intelligence Engine. "
    "You analyze website content and extract structured business profiles. "
    "Always respond with ONLY valid JSON — no markdown fences, no explanation."
)

ANALYSIS_PROMPT = """\
Analyze the following website content and extract a structured business profile.

Return ONLY valid JSON with this exact schema:
{
  "business_profile": {
    "sector": "<e.g. E-commerce, Healthcare, Education, F&B, Real Estate, SaaS, Agency>",
    "description": "<2-3 sentence business description>",
    "goals": ["<goal1>", "<goal2>", "<goal3>"]
  },
  "localization": {
    "primary_language": "<ar or en>",
    "secondary_languages": ["<lang_code>"]
  },
  "knowledge_assets": {
    "faqs": [
      {"q": "<question>", "a": "<answer>"}
    ],
    "tone_of_voice": "<professional | friendly | formal | casual | luxury>",
    "brand_keywords": ["<keyword1>", "<keyword2>"]
  }
}

Rules:
- Extract up to 5 FAQs that a customer would likely ask about this business.
- If the content is in Arabic, write FAQs and description in Arabic.
- brand_keywords: 5-10 words that define the brand identity.
- If you cannot determine a field, use a reasonable default.

--- WEBSITE CONTENT ---
"""


# ═══════════════════════════════════════════════════════════════
# PHASE 1: Firecrawl Scrape
# ═══════════════════════════════════════════════════════════════

async def _scrape_url(url: str) -> dict:
    """Scrape a single page via Firecrawl. Returns markdown + metadata."""
    if not FIRECRAWL_KEY:
        raise RuntimeError("FIRECRAWL_API_KEY not configured")

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{FIRECRAWL_BASE}/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_KEY}"},
            json={"url": url, "formats": ["markdown", "links"]},
        )
        if r.status_code == 402:
            raise RuntimeError("Firecrawl quota exceeded — check your plan")
        if r.status_code == 429:
            raise RuntimeError("Firecrawl rate limit — try again shortly")
        r.raise_for_status()
        return r.json().get("data", {})


def _find_about_url(links: list, base_url: str) -> str | None:
    """Find an /about page from scraped links."""
    base_domain = re.sub(r"https?://", "", base_url).rstrip("/").lower()
    for link in (links or []):
        if not isinstance(link, str):
            continue
        lower = link.lower()
        if base_domain not in lower:
            continue
        if any(seg in lower for seg in ["/about", "/who-we-are", "/من-نحن", "/نبذة"]):
            return link
    return None


async def scrape_website(url: str) -> tuple[str, dict]:
    """Scrape homepage + about page. Returns (combined_markdown, metadata)."""
    log.info("[ingest] Scraping homepage: %s", url)
    main_data = await _scrape_url(url)
    content = main_data.get("markdown", "")
    metadata = main_data.get("metadata", {})

    links = main_data.get("links", [])
    about_url = _find_about_url(links, url)

    if about_url:
        log.info("[ingest] Found about page: %s", about_url)
        try:
            about_data = await _scrape_url(about_url)
            about_md = about_data.get("markdown", "")
            if about_md:
                content += f"\n\n--- ABOUT PAGE ({about_url}) ---\n\n{about_md}"
        except Exception as exc:
            log.warning("[ingest] About page scrape failed: %s", exc)

    if not content.strip():
        raise RuntimeError(f"No readable content extracted from {url}")

    log.info("[ingest] Scraped %d chars (about page: %s)", len(content), "yes" if about_url else "no")
    return content, metadata


# ═══════════════════════════════════════════════════════════════
# PHASE 2: Claude AI Analysis
# ═══════════════════════════════════════════════════════════════

def _extract_json(text: str) -> dict:
    """Extract JSON from Claude's response, handling markdown fences."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


async def analyze_with_claude(content: str) -> dict:
    """Send scraped content to Claude for structured business analysis."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    truncated = content[:MAX_CONTENT_CHARS]

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2500,
                "system": ANALYSIS_SYSTEM,
                "messages": [
                    {"role": "user", "content": ANALYSIS_PROMPT + truncated},
                ],
            },
        )
        if r.status_code == 401:
            raise RuntimeError("Anthropic API key invalid or expired")
        if r.status_code == 429:
            raise RuntimeError("Claude rate limit — try again shortly")
        r.raise_for_status()

        resp = r.json()
        text_block = resp.get("content", [{}])[0].get("text", "")
        if not text_block:
            raise RuntimeError("Claude returned empty response")

        try:
            analysis = _extract_json(text_block)
        except json.JSONDecodeError as exc:
            log.error("[ingest] Claude JSON parse failed: %s\nRaw: %s", exc, text_block[:500])
            raise RuntimeError(f"Claude returned invalid JSON: {str(exc)[:100]}")

    log.info("[ingest] Claude analysis complete — sector: %s",
             analysis.get("business_profile", {}).get("sector", "?"))
    return analysis


# ═══════════════════════════════════════════════════════════════
# PHASE 3: Persist to Postgres (Upsert)
# ═══════════════════════════════════════════════════════════════

async def persist_analysis(
    project_id: str, url: str, analysis: dict, project_name: str = "Siyadah Client"
) -> dict:
    """Upsert analysis results into ProjectIdentity + KnowledgeAsset.

    Returns a summary dict of what was saved.
    """
    from database import async_session
    from models import KnowledgeAsset, Project, ProjectIdentity

    if not async_session:
        raise RuntimeError("Database not configured (DATABASE_URL missing)")

    bp = analysis.get("business_profile", {})
    loc = analysis.get("localization", {})
    ka = analysis.get("knowledge_assets", {})

    async with async_session() as session:
        async with session.begin():
            proj = (await session.execute(
                select(Project).where(Project.project_id == project_id)
            )).scalar_one_or_none()

            if not proj:
                proj = Project(project_id=project_id, name=project_name)
                session.add(proj)
                await session.flush()

            # Upsert ProjectIdentity
            identity = (await session.execute(
                select(ProjectIdentity).where(ProjectIdentity.project_id == project_id)
            )).scalar_one_or_none()

            if not identity:
                identity = ProjectIdentity(project_id=project_id)
                session.add(identity)

            identity.sector = bp.get("sector") or identity.sector
            identity.business_description = bp.get("description") or identity.business_description
            identity.language = loc.get("primary_language") or identity.language or "en"
            identity.website_url = url
            identity.absorbed_at = datetime.now(timezone.utc)

            # Upsert KnowledgeAsset
            knowledge = (await session.execute(
                select(KnowledgeAsset).where(KnowledgeAsset.project_id == project_id)
            )).scalar_one_or_none()

            if not knowledge:
                knowledge = KnowledgeAsset(project_id=project_id)
                session.add(knowledge)

            knowledge.faqs = ka.get("faqs") or knowledge.faqs or []
            knowledge.tone_of_voice = ka.get("tone_of_voice") or knowledge.tone_of_voice
            knowledge.brand_keywords = ka.get("brand_keywords") or knowledge.brand_keywords or []

    saved = {
        "sector": identity.sector,
        "language": identity.language,
        "description": identity.business_description,
        "website_url": url,
        "tone_of_voice": knowledge.tone_of_voice,
        "faqs_count": len(knowledge.faqs) if knowledge.faqs else 0,
        "brand_keywords_count": len(knowledge.brand_keywords) if knowledge.brand_keywords else 0,
    }
    log.info("[ingest] Persisted for project %s: sector=%s, faqs=%d",
             project_id, saved["sector"], saved["faqs_count"])
    return saved


# ═══════════════════════════════════════════════════════════════
# PREVIEW: Scrape + Analyze (no DB write)
# ═══════════════════════════════════════════════════════════════

async def preview_website(url: str) -> dict[str, Any]:
    """Preview pipeline: Scrape → Analyze → return results without persisting.

    Used by the Smart Onboarding flow so the client can review
    the analysis before committing to a full registration.
    """
    content, metadata = await scrape_website(url)
    analysis = await analyze_with_claude(content)
    site_title = metadata.get("title", url)

    bp = analysis.get("business_profile", {})
    ka = analysis.get("knowledge_assets", {})
    lang = analysis.get("localization", {}).get("primary_language", "en")

    faqs_preview = [faq.get("q", "") for faq in (ka.get("faqs") or [])[:3]]

    return {
        "status": "preview",
        "url": url,
        "site_title": site_title,
        "analysis": analysis,
        "profile": {
            "sector": bp.get("sector"),
            "description": bp.get("description"),
            "goals": bp.get("goals", []),
            "language": lang,
            "tone_of_voice": ka.get("tone_of_voice"),
            "faqs_count": len(ka.get("faqs") or []),
            "brand_keywords": ka.get("brand_keywords", []),
            "faqs_preview": faqs_preview,
        },
        "_hint": "Preview complete. Call POST /v2/saas/register with this analysis to save.",
    }


# ═══════════════════════════════════════════════════════════════
# ORCHESTRATOR: Full Pipeline
# ═══════════════════════════════════════════════════════════════

async def ingest_website(url: str, project_id: str) -> dict[str, Any]:
    """Full absorption pipeline: Scrape → Analyze → Persist → Wow Response."""

    # Phase 1: Scrape
    content, metadata = await scrape_website(url)

    # Phase 2: AI Analysis
    analysis = await analyze_with_claude(content)

    # Phase 3: Persist
    site_title = metadata.get("title", url)
    saved = await persist_analysis(project_id, url, analysis, project_name=site_title)

    # Build the Wow Response
    bp = analysis.get("business_profile", {})
    ka = analysis.get("knowledge_assets", {})
    lang = analysis.get("localization", {}).get("primary_language", "en")

    faqs_preview = []
    for faq in (ka.get("faqs") or [])[:3]:
        faqs_preview.append(faq.get("q", ""))

    if lang == "ar":
        summary = (
            f"تم امتصاص موقع «{site_title}» بنجاح. "
            f"القطاع: {bp.get('sector', 'غير محدد')}. "
            f"تم استخراج {saved['faqs_count']} أسئلة شائعة "
            f"و{saved['brand_keywords_count']} كلمة مفتاحية. "
            f"نبرة الصوت: {ka.get('tone_of_voice', 'احترافية')}."
        )
        hint = (
            "تم امتصاص الهوية بنجاح. "
            "راجع إعداداتك عبر GET /v2/project/{project_id}/memory. "
            "الخطوة التالية: تفعيل القواعد الذكية للردود التلقائية."
        )
    else:
        summary = (
            f"Successfully absorbed «{site_title}». "
            f"Sector: {bp.get('sector', 'Unknown')}. "
            f"Extracted {saved['faqs_count']} FAQs "
            f"and {saved['brand_keywords_count']} brand keywords. "
            f"Tone: {ka.get('tone_of_voice', 'professional')}."
        )
        hint = (
            "Identity absorbed. "
            "Review your settings at GET /v2/project/{project_id}/memory. "
            "Next step: configure autonomous response rules."
        )

    return {
        "status": "absorbed",
        "project_id": project_id,
        "url": url,
        "site_title": site_title,
        "profile": {
            "sector": bp.get("sector"),
            "description": bp.get("description"),
            "goals": bp.get("goals", []),
            "language": lang,
            "tone_of_voice": ka.get("tone_of_voice"),
            "faqs_count": saved["faqs_count"],
            "brand_keywords": ka.get("brand_keywords", []),
            "faqs_preview": faqs_preview,
        },
        "summary": summary,
        "_hint": hint,
        "memory_status": "complete",
        "absorbed_at": datetime.now(timezone.utc).isoformat(),
    }
