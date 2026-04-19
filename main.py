import json
import os
import uuid
from typing import Optional

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


def anthropic_client() -> Anthropic:
    return Anthropic(api_key=ANTHROPIC_API_KEY)


def supabase_client() -> Optional[Client]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class ParseRfqRequest(BaseModel):
    text: str


PARSE_SYSTEM = """You extract structured procurement fields from a buyer's natural-language RFQ description.
Return ONLY a JSON object with these keys (use null if truly unknown):
- location: ISO3 country code (e.g. "USA", "IND", "DEU")
- product_category: short noun phrase describing what is being sourced
- quantity: integer units
- budget_min: integer USD (lower bound of target price, total or per-unit — infer from context)
- budget_max: integer USD (upper bound)
- timeline_weeks: integer weeks until delivery needed

No prose, no markdown fences. JSON only."""


@app.get("/")
def health():
    return {"ok": True, "service": "vendrsurf-backend"}


@app.post("/parse-rfq")
def parse_rfq(req: ParseRfqRequest):
    if not ANTHROPIC_API_KEY:
        return {"error": "config", "message": "ANTHROPIC_API_KEY not set"}
    try:
        msg = anthropic_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=PARSE_SYSTEM,
            messages=[{"role": "user", "content": req.text}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        fields = json.loads(raw)
        return {"fields": fields}
    except json.JSONDecodeError as e:
        return {"error": "parse", "message": f"model did not return JSON: {e}"}
    except Exception as e:
        return {"error": "upstream", "message": str(e)}


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

POC_TITLE_KEYWORDS = ["procurement", "supply", "sourcing"]


def crust_company_search(client: httpx.Client, query: str, country: Optional[str]):
    payload: dict = {"query": query, "limit": 10}
    if country:
        payload["filters"] = {"hq_country": country}
    r = client.post(f"{CRUST_BASE}/company/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    return data.get("results") or data.get("companies") or []


def crust_person_search(client: httpx.Client, company_id: str):
    payload = {
        "filters": {
            "crustdata_company_id": company_id,
            "title_contains_any": POC_TITLE_KEYWORDS,
        },
        "limit": 3,
    }
    try:
        r = client.post(f"{CRUST_BASE}/person/search", json=payload, headers=CRUST_HEADERS, timeout=30.0)
        r.raise_for_status()
        data = r.json()
        return data.get("results") or data.get("people") or []
    except Exception:
        return []


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
                company_id = str(c.get("id") or c.get("crustdata_company_id") or "")
                name = c.get("name") or c.get("company_name") or "Unknown"
                location = c.get("hq_country") or c.get("country") or c.get("location") or None
                employees = c.get("employee_count") or c.get("headcount") or None

                pocs = crust_person_search(client, company_id) if company_id else []
                contact = None
                if pocs:
                    p = pocs[0]
                    contact = {
                        "name": p.get("name") or p.get("full_name"),
                        "title": p.get("title"),
                        "email": p.get("email"),
                        "linkedin": p.get("linkedin_url"),
                    }

                vendor_id = f"v-{uuid.uuid4().hex[:12]}"
                row = {
                    "id": vendor_id,
                    "rfq_id": req.rfq_id,
                    "name": name,
                    "location": location,
                    "employees": str(employees) if employees is not None else None,
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
