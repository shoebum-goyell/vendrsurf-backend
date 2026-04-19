import json
import os
import uuid
from typing import Any, Optional

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import Client, create_client

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CRUST_DATA_API_KEY = os.getenv("CRUST_DATA_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

app = FastAPI(title="vendrsurf-backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


def supabase_client() -> Optional[Client]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def anthropic_client() -> Optional[Anthropic]:
    if not ANTHROPIC_API_KEY:
        return None
    return Anthropic(api_key=ANTHROPIC_API_KEY)


@app.get("/")
def health():
    return {"ok": True, "service": "vendrsurf-backend"}


class DiscoverVendorsRequest(BaseModel):
    rfq_id: str
    location: Optional[str] = None
    product_category: str
    quantity: Optional[int] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    timeline_weeks: Optional[int] = None


CRUST_BASE = "https://api.crustdata.com"
CRUST_HEADERS = {
    "Authorization": f"Token {CRUST_DATA_API_KEY}",
    "x-api-version": "2025-11-01",
    "Content-Type": "application/json",
}

# Tunable mapping from RFQ quantity to preferred supplier headcount range.
# (min_employees, max_employees or None for open-ended).
QTY_TO_HEADCOUNT: list = [
    (0, 1_000, 10, 200),
    (1_000, 10_000, 50, 1_000),
    (10_000, 100_000, 200, 5_000),
    (100_000, None, 500, None),
]


def headcount_range_for_quantity(qty: Optional[int]) -> Optional[tuple]:
    if qty is None:
        return None
    for q_lo, q_hi, h_lo, h_hi in QTY_TO_HEADCOUNT:
        if qty >= q_lo and (q_hi is None or qty < q_hi):
            return (h_lo, h_hi)
    return None


SEARCH_PLAN_PROMPT = """You help a procurement tool find suppliers on Crust Data.

Given an RFQ, return strict JSON with three arrays:
- "categories": 2-4 broad Crust Data taxonomy terms for company category search. Example for "custom PCBs": ["PCB", "printed circuit board", "electronics manufacturing"].
- "specialities": 3-6 more specific LinkedIn-style speciality terms the supplier would list. Example for "custom PCBs": ["pcb design", "pcb assembly", "smt", "through-hole", "rigid-flex pcb"].
- "title_keywords": 3-6 job title fragments for procurement/sales POCs at suppliers. Example: ["procurement", "sourcing", "supply chain", "buyer", "sales", "business development"].

Return only JSON, no prose. Keep all terms lowercase.

RFQ:
{rfq}
"""


def build_search_plan(rfq: DiscoverVendorsRequest) -> dict:
    fallback = {
        "categories": [rfq.product_category],
        "specialities": [],
        "title_keywords": ["procurement", "supply", "sourcing", "purchasing", "buyer", "sales"],
    }
    client = anthropic_client()
    if client is None:
        return fallback
    rfq_text = json.dumps({
        "product_category": rfq.product_category,
        "location": rfq.location,
        "quantity": rfq.quantity,
        "budget_min": rfq.budget_min,
        "budget_max": rfq.budget_max,
        "timeline_weeks": rfq.timeline_weeks,
    })
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": SEARCH_PLAN_PROMPT.format(rfq=rfq_text)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        plan = json.loads(text)
        cats = [str(k).strip().lower() for k in plan.get("categories") or [] if str(k).strip()]
        specs = [str(k).strip().lower() for k in plan.get("specialities") or [] if str(k).strip()]
        titles = [str(r).strip().lower() for r in plan.get("title_keywords") or [] if str(r).strip()]
        return {
            "categories": cats or fallback["categories"],
            "specialities": specs,
            "title_keywords": titles or fallback["title_keywords"],
        }
    except Exception:
        return fallback


def _leaf(field: str, op: str, value: Any) -> dict:
    return {"field": field, "type": op, "value": value, "op": "and", "conditions": []}


def _group(conditions: list, op: str = "and") -> dict:
    return {"field": "", "type": "", "value": "", "op": op, "conditions": conditions}


def crust_company_search(
    client: httpx.Client,
    category: Optional[str],
    speciality: Optional[str],
    country: Optional[str],
    headcount: Optional[tuple],
) -> list:
    term_group = []
    if category:
        term_group.append(_leaf("taxonomy.categories", "(.)", category))
    if speciality:
        term_group.append(_leaf("taxonomy.professional_network_specialities", "(.)", speciality))
    conds: list = []
    if len(term_group) == 1:
        conds.append(term_group[0])
    elif term_group:
        conds.append(_group(term_group, op="or"))
    if country:
        conds.append(_leaf("locations.country", "=", country))
    if headcount:
        lo, hi = headcount
        if lo is not None:
            conds.append(_leaf("headcount.total", ">=", lo))
        if hi is not None:
            conds.append(_leaf("headcount.total", "<=", hi))
    payload = {"filters": _group(conds), "limit": 10}
    r = client.post(f"{CRUST_BASE}/company/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
    r.raise_for_status()
    return r.json().get("companies", [])


def search_companies_multi(
    client: httpx.Client,
    categories: list,
    specialities: list,
    country: Optional[str],
    headcount: Optional[tuple],
) -> list:
    seen: dict = {}

    def _run(cats: list, specs: list, ctry: Optional[str], hc: Optional[tuple]) -> None:
        # specialities first — more granular matches
        for sp in specs:
            try:
                for c in crust_company_search(client, None, sp, ctry, hc):
                    cid = c.get("crustdata_company_id")
                    if cid and cid not in seen:
                        seen[cid] = c
            except Exception:
                continue
        for kw in cats:
            try:
                for c in crust_company_search(client, kw, None, ctry, hc):
                    cid = c.get("crustdata_company_id")
                    if cid and cid not in seen:
                        seen[cid] = c
            except Exception:
                continue

    _run(categories, specialities, country, headcount)
    if len(seen) < 3 and headcount:
        _run(categories, specialities, country, None)
    if len(seen) < 3 and country:
        _run(categories, specialities, None, None)
    return list(seen.values())[:10]


def crust_person_search_for_company(
    client: httpx.Client, company_id: int, title_keywords: list
) -> list:
    title_conds = [_leaf("experience.employment_details.current.title", "(.)", t) for t in title_keywords]
    conds: list = [_leaf("experience.employment_details.company_id", "=", company_id)]
    if title_conds:
        conds.append(_group(title_conds, op="or") if len(title_conds) > 1 else title_conds[0])
    payload = {"filters": _group(conds), "limit": 10}
    try:
        r = client.post(f"{CRUST_BASE}/person/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
        r.raise_for_status()
        return r.json().get("profiles", [])
    except Exception:
        # fall back without title filter — better a less-targeted POC than none
        try:
            r = client.post(
                f"{CRUST_BASE}/person/search",
                json={"filters": _group([_leaf("experience.employment_details.company_id", "=", company_id)]), "limit": 10},
                headers=CRUST_HEADERS,
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json().get("profiles", [])
        except Exception:
            return []


def pick_poc(profiles: list, company_id: int, title_keywords: list) -> Optional[dict]:
    ranked: list = []
    for p in profiles:
        current = (p.get("experience", {}).get("employment_details", {}).get("current") or [])
        if not any(e.get("crustdata_company_id") == company_id for e in current):
            continue
        title = (p.get("basic_profile", {}).get("current_title") or "").lower()
        contact = p.get("contact", {}) or {}
        score = 0
        if any(t in title for t in title_keywords):
            score += 10
        if contact.get("has_business_email"):
            score += 5
        if contact.get("has_phone_number"):
            score += 2
        ranked.append((score, p))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    return _format_poc(ranked[0][1])


def _format_poc(p: dict) -> dict:
    bp = p.get("basic_profile", {}) or {}
    social = p.get("social_handles", {}) or {}
    contact = p.get("contact", {}) or {}
    return {
        "name": bp.get("name"),
        "title": bp.get("current_title"),
        "linkedin": (social.get("professional_network_identifier") or {}).get("profile_url"),
        "has_business_email": bool(contact.get("has_business_email")),
    }


@app.post("/discover-vendors")
def discover_vendors(req: DiscoverVendorsRequest):
    if not CRUST_DATA_API_KEY:
        return {"error": "config", "message": "CRUST_DATA_API_KEY not set"}
    sb = supabase_client()
    if sb is None:
        return {"error": "config", "message": "Supabase not configured"}

    plan = build_search_plan(req)
    categories = plan["categories"]
    specialities = plan["specialities"]
    title_keywords = plan["title_keywords"]
    headcount = headcount_range_for_quantity(req.quantity)

    try:
        with httpx.Client() as client:
            companies = search_companies_multi(client, categories, specialities, req.location, headcount)

            vendors_out = []
            for c in companies:
                basic = c.get("basic_info", {}) or {}
                loc = c.get("locations", {}) or {}
                headcount_info = c.get("headcount", {}) or {}
                company_id = c.get("crustdata_company_id")

                profiles = crust_person_search_for_company(client, company_id, title_keywords) if company_id else []
                contact = pick_poc(profiles, company_id, title_keywords) if profiles else None

                row = {
                    "id": f"v-{uuid.uuid4().hex[:12]}",
                    "rfq_id": req.rfq_id,
                    "name": basic.get("name") or "Unknown",
                    "location": loc.get("country"),
                    "employees": str(headcount_info.get("total")) if headcount_info.get("total") is not None else basic.get("employee_count_range"),
                    "contact": contact,
                    "status": "discovered",
                }
                sb.table("vendors").upsert(row).execute()
                vendors_out.append(row)

            return {
                "vendors": vendors_out,
                "search_plan": {**plan, "headcount_range": list(headcount) if headcount else None},
            }
    except httpx.HTTPStatusError as e:
        return {"error": "upstream", "message": f"Crust Data {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": "upstream", "message": str(e)}
