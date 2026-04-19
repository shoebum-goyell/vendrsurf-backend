import os
import uuid
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import Client, create_client

load_dotenv()

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


@app.get("/")
def health():
    return {"ok": True, "service": "vendrsurf-backend"}


class DiscoverVendorsRequest(BaseModel):
    rfq_id: str
    location: Optional[str] = None
    product_category: str


CRUST_BASE = "https://api.crustdata.com"
CRUST_HEADERS = {
    "Authorization": f"Token {CRUST_DATA_API_KEY}",
    "x-api-version": "2025-11-01",
    "Content-Type": "application/json",
}

POC_TITLE_KEYWORDS = ("procurement", "supply", "sourcing", "purchasing", "buyer")


def _leaf(field: str, op: str, value: Any) -> dict:
    return {"field": field, "type": op, "value": value, "op": "and", "conditions": []}


def _group(conditions: list, op: str = "and") -> dict:
    return {"field": "", "type": "", "value": "", "op": op, "conditions": conditions}


def crust_company_search(client: httpx.Client, category: str, country: Optional[str]) -> list:
    conds = [_leaf("taxonomy.categories", "(.)", category)]
    if country:
        conds.append(_leaf("locations.country", "=", country))
    payload = {"filters": _group(conds), "limit": 10}
    r = client.post(f"{CRUST_BASE}/company/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
    r.raise_for_status()
    return r.json().get("companies", [])


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


def pick_poc(profiles: list, company_id: int) -> Optional[dict]:
    best = None
    for p in profiles:
        current = (p.get("experience", {}).get("employment_details", {}).get("current") or [])
        active_at_co = any(e.get("crustdata_company_id") == company_id for e in current)
        if not active_at_co:
            continue
        title = (p.get("basic_profile", {}).get("current_title") or "").lower()
        if any(k in title for k in POC_TITLE_KEYWORDS):
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

    try:
        with httpx.Client() as client:
            companies = crust_company_search(client, req.product_category, req.location)
            if len(companies) < 3 and req.location:
                companies = crust_company_search(client, req.product_category, None)
            companies = companies[:10]

            vendors_out = []
            for c in companies:
                basic = c.get("basic_info", {}) or {}
                loc = c.get("locations", {}) or {}
                headcount = c.get("headcount", {}) or {}
                company_id = c.get("crustdata_company_id")

                profiles = crust_person_search_for_company(client, company_id) if company_id else []
                contact = pick_poc(profiles, company_id) if profiles else None

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

            return {"vendors": vendors_out}
    except httpx.HTTPStatusError as e:
        return {"error": "upstream", "message": f"Crust Data {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": "upstream", "message": str(e)}
