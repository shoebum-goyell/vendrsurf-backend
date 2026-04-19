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

SEARCH_PLAN_PROMPT = """You help a procurement tool find suppliers on Crust Data.

Given an RFQ, return strict JSON with two arrays:
- "keywords": 2-4 short Crust Data taxonomy-style terms for company search (broad industry/category terms, not product SKUs). Example: for "custom PCBs" -> ["PCB", "printed circuit board", "electronics manufacturing"].
- "roles": 3-5 job title fragments for procurement POCs at suppliers. Example: ["Procurement", "Sourcing", "Supply Chain", "Buyer", "Purchasing"].

Return only JSON, no prose. Keep terms lowercase.

RFQ:
{rfq}
"""


def build_search_plan(rfq: DiscoverVendorsRequest) -> dict:
    fallback = {
        "keywords": [rfq.product_category],
        "roles": ["procurement", "supply", "sourcing", "purchasing", "buyer"],
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
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": SEARCH_PLAN_PROMPT.format(rfq=rfq_text)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        plan = json.loads(text)
        kws = [str(k).strip().lower() for k in plan.get("keywords") or [] if str(k).strip()]
        roles = [str(r).strip().lower() for r in plan.get("roles") or [] if str(r).strip()]
        return {
            "keywords": kws or fallback["keywords"],
            "roles": roles or fallback["roles"],
        }
    except Exception:
        return fallback


def _leaf(field: str, op: str, value: Any) -> dict:
    return {"field": field, "type": op, "value": value, "op": "and", "conditions": []}


def _group(conditions: list, op: str = "and") -> dict:
    return {"field": "", "type": "", "value": "", "op": op, "conditions": conditions}


def crust_company_search(client: httpx.Client, keyword: str, country: Optional[str]) -> list:
    conds = [_leaf("taxonomy.categories", "(.)", keyword)]
    if country:
        conds.append(_leaf("locations.country", "=", country))
    payload = {"filters": _group(conds), "limit": 10}
    r = client.post(f"{CRUST_BASE}/company/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
    r.raise_for_status()
    return r.json().get("companies", [])


def search_companies_multi(client: httpx.Client, keywords: list, country: Optional[str]) -> list:
    seen: dict = {}
    for kw in keywords:
        try:
            for c in crust_company_search(client, kw, country):
                cid = c.get("crustdata_company_id")
                if cid and cid not in seen:
                    seen[cid] = c
        except Exception:
            continue
    if len(seen) < 3 and country:
        for kw in keywords:
            try:
                for c in crust_company_search(client, kw, None):
                    cid = c.get("crustdata_company_id")
                    if cid and cid not in seen:
                        seen[cid] = c
            except Exception:
                continue
    return list(seen.values())[:10]


def crust_person_search_for_company(client: httpx.Client, company_id: int) -> list:
    payload = {
        "filters": _group([
            _leaf("experience.employment_details.company_id", "=", company_id),
        ]),
        "limit": 10,
    }
    try:
        r = client.post(f"{CRUST_BASE}/person/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
        r.raise_for_status()
        return r.json().get("profiles", [])
    except Exception:
        return []


def pick_poc(profiles: list, company_id: int, roles: list) -> Optional[dict]:
    best = None
    for p in profiles:
        current = (p.get("experience", {}).get("employment_details", {}).get("current") or [])
        active_at_co = any(e.get("crustdata_company_id") == company_id for e in current)
        if not active_at_co:
            continue
        title = (p.get("basic_profile", {}).get("current_title") or "").lower()
        if any(r in title for r in roles):
            return _format_poc(p)
        if best is None:
            best = p
    return _format_poc(best) if best else None


def _format_poc(p: dict) -> dict:
    bp = p.get("basic_profile", {}) or {}
    social = p.get("social_handles", {}) or {}
    return {
        "name": bp.get("name"),
        "title": bp.get("current_title"),
        "linkedin": (social.get("professional_network_identifier") or {}).get("profile_url"),
    }


@app.post("/discover-vendors")
def discover_vendors(req: DiscoverVendorsRequest):
    if not CRUST_DATA_API_KEY:
        return {"error": "config", "message": "CRUST_DATA_API_KEY not set"}
    sb = supabase_client()
    if sb is None:
        return {"error": "config", "message": "Supabase not configured"}

    plan = build_search_plan(req)
    keywords = plan["keywords"]
    roles = plan["roles"]

    try:
        with httpx.Client() as client:
            companies = search_companies_multi(client, keywords, req.location)

            vendors_out = []
            for c in companies:
                basic = c.get("basic_info", {}) or {}
                loc = c.get("locations", {}) or {}
                headcount = c.get("headcount", {}) or {}
                company_id = c.get("crustdata_company_id")

                profiles = crust_person_search_for_company(client, company_id) if company_id else []
                contact = pick_poc(profiles, company_id, roles) if profiles else None

                row = {
                    "id": f"v-{uuid.uuid4().hex[:12]}",
                    "rfq_id": req.rfq_id,
                    "name": basic.get("name") or "Unknown",
                    "location": loc.get("country"),
                    "employees": str(headcount.get("total")) if headcount.get("total") is not None else basic.get("employee_count_range"),
                    "contact": contact,
                    "status": "discovered",
                }
                sb.table("vendors").upsert(row).execute()
                vendors_out.append(row)

            return {"vendors": vendors_out, "search_plan": plan}
    except httpx.HTTPStatusError as e:
        return {"error": "upstream", "message": f"Crust Data {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": "upstream", "message": str(e)}
