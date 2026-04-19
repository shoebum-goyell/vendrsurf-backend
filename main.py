import json
import os
import random
import uuid
from typing import Any, Optional

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client

from vapi import trigger_call, handle_webhook

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CRUST_DATA_API_KEY = os.getenv("CRUST_DATA_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID", "")

# GLD-18: hard-coded test number for the entire end-to-end demo flow.
# All outbound calls go here regardless of vendor contact details.
HARDCODED_VENDOR_PHONE = "+16506915431"

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


class ParseRFQRequest(BaseModel):
    transcript: str


PARSE_RFQ_PROMPT = """You extract structured RFQ fields from a procurement buyer's voice transcript. The output feeds a voice-AI that will negotiate with vendors, so precision matters — never guess.

Return strict JSON with exactly these keys. Use null for anything not stated.

Extraction rules per field:
- product_description (string): free-text spec — grade, dimensions, material, tolerances. Extract VERBATIM from the buyer's words; do NOT summarize or paraphrase. This is the #1 anchor for any quote.
- product_category (string): short category label for Crust Data search (e.g. "CNC-machined aluminum enclosures", "custom PCBs", "plastic bottles"). Infer from product_description if not said directly.
- location (string): country the RFQ is sourced from (3-letter ISO code like "USA", "IND", "CHN") if stated.
- quantity (integer): numeric quantity.
- unit_of_measure (string, one of: units | kg | tons | meters | liters | pieces): pairs with quantity. Infer from product type if buyer omits (plastic pellets → kg, bottles → units, metal parts → pieces).
- target_unit_price (number): per-unit anchor price. Extract ONLY if buyer states per-unit explicitly ("under $0.50 a piece"). NEVER divide total budget by quantity.
- budget_min (number): total budget lower bound if stated.
- budget_max (number): total budget upper bound if stated.
- delivery_destination (string): "City, Country" more specific than location. If buyer gives only country, still record it.
- timeline_weeks (integer): delivery window in weeks.
- certifications (string[]): RoHS, ISO 9001, REACH, FDA, UL, etc. Extract acronym matches only; do NOT infer from product category.
- payment_terms (string, one of: advance | net_30 | net_60 | net_90): extract only if stated; leave null otherwise.
- sample_required (boolean): true if buyer mentions "sample", "prototype", "swatch"; default false.
- recurring (boolean): true if buyer says "monthly", "quarterly", "ongoing", "repeat"; default false (one-time).

Return only JSON, no prose.

Transcript:
{transcript}
"""


def _coerce_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (ValueError, TypeError):
        return None


def _coerce_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (ValueError, TypeError):
        return None


def _coerce_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _coerce_enum(v: Any, allowed: set) -> Optional[str]:
    s = _coerce_str(v)
    if s is None:
        return None
    s = s.lower()
    return s if s in allowed else None


@app.post("/parse-rfq")
def parse_rfq(req: ParseRFQRequest):
    client = anthropic_client()
    if client is None:
        return {"error": "config", "message": "ANTHROPIC_API_KEY not set"}
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": PARSE_RFQ_PROMPT.format(transcript=req.transcript)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        raw = json.loads(text)
    except Exception as e:
        return {"error": "parse", "message": str(e)}

    uom_allowed = {"units", "kg", "tons", "meters", "liters", "pieces"}
    pay_allowed = {"advance", "net_30", "net_60", "net_90"}
    certs_raw = raw.get("certifications") or []
    certs = [s for s in (_coerce_str(c) for c in certs_raw) if s]

    fields = {
        "product_description": _coerce_str(raw.get("product_description")),
        "product_category": _coerce_str(raw.get("product_category")),
        "location": _coerce_str(raw.get("location")),
        "quantity": _coerce_int(raw.get("quantity")),
        "unit_of_measure": _coerce_enum(raw.get("unit_of_measure"), uom_allowed),
        "target_unit_price": _coerce_float(raw.get("target_unit_price")),
        "budget_min": _coerce_float(raw.get("budget_min")),
        "budget_max": _coerce_float(raw.get("budget_max")),
        "delivery_destination": _coerce_str(raw.get("delivery_destination")),
        "timeline_weeks": _coerce_int(raw.get("timeline_weeks")),
        "certifications": certs,
        "payment_terms": _coerce_enum(raw.get("payment_terms"), pay_allowed),
        "sample_required": _coerce_bool(raw.get("sample_required")),
        "recurring": _coerce_bool(raw.get("recurring")),
    }
    return {"fields": fields}


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


class CallRequest(BaseModel):
    assistant_id: str
    vendor_phone: str = Field(description="E.164 format, e.g. +14155551234")
    contact_first_name: str
    vendor_company: str
    buyer_company: str
    buyer_one_liner: str
    rfq_one_liner: str
    preferred_process: str
    preferred_material: str
    target_quantity_phrase: str = Field(description="Spoken form, e.g. 'five hundred units'")
    eau_phrase: str = Field(description="Spoken form, e.g. 'around ten thousand per year'")
    key_constraint: str
    required_certifications: str = Field(default="none")
    email_followup_contact: str = Field(description="Spoken form, e.g. 'kaustubh at vendrsurf dot com'")
    rfq_id: Optional[str] = None
    vendor_id: Optional[str] = None
    callback_url: Optional[str] = Field(default=None, description="URL to POST webhook results to when call events arrive")


class CallResponse(BaseModel):
    call_id: str
    status: str = "triggered"
    message: str = "Call initiated successfully"


class CallVendorRequest(BaseModel):
    rfq_id: str
    vendor_id: str


# Spoken-form helpers — Vapi assistant reads these out loud, so digits/units
# need to be phrased naturally.
_NUM_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def _qty_phrase(qty: Optional[int], uom: Optional[str]) -> str:
    if qty is None:
        return "an unspecified quantity"
    if qty < 11:
        word = _NUM_WORDS[qty]
    elif qty < 1000:
        word = f"{qty}"
    elif qty < 1_000_000:
        word = f"{qty / 1000:.1f}".rstrip("0").rstrip(".") + " thousand"
    else:
        word = f"{qty / 1_000_000:.1f}".rstrip("0").rstrip(".") + " million"
    unit = uom or "units"
    return f"{word} {unit}"


def _eau_phrase(qty: Optional[int], uom: Optional[str], recurring: bool) -> str:
    if qty is None:
        return "ongoing volume to be confirmed"
    base = _qty_phrase(qty, uom)
    return f"around {base} per year" if recurring else f"a one-time order of {base}"


def _rfq_one_liner(rfq: dict) -> str:
    desc = rfq.get("product_description") or rfq.get("product_category") or rfq.get("title") or "a custom part"
    return desc


def _key_constraint(rfq: dict) -> str:
    parts = []
    if rfq.get("tolerance"):
        parts.append(f"tolerance of {rfq['tolerance']}")
    if rfq.get("finish"):
        parts.append(f"finish: {rfq['finish']}")
    if rfq.get("max_lead_time_days"):
        parts.append(f"delivery within {rfq['max_lead_time_days']} days")
    elif rfq.get("timeline_weeks"):
        parts.append(f"delivery within {rfq['timeline_weeks']} weeks")
    if rfq.get("target_unit_price"):
        parts.append(f"target unit price around {rfq['target_unit_price']} dollars")
    return "; ".join(parts) or "standard commercial terms"


def _build_call_variables(rfq: dict, vendor: dict) -> dict[str, str]:
    contact = vendor.get("contact") or {}
    contact_first_name = (contact.get("name") or "there").split()[0] if contact.get("name") else "there"
    certs = rfq.get("certifications") or []
    certs_phrase = ", ".join(certs) if certs else "none"
    buyer_name = rfq.get("workspace_name") or "VendrSurf"
    buyer_email_raw = rfq.get("current_user_email") or "team@vendrsurf.com"
    # Spoken email: replace @ with " at " and . with " dot "
    buyer_email_spoken = buyer_email_raw.replace("@", " at ").replace(".", " dot ")

    return {
        "buyer_company": buyer_name,
        "buyer_one_liner": f"{buyer_name} is sourcing {rfq.get('product_category') or 'custom manufacturing'}.",
        "vendor_company": vendor.get("name") or "your company",
        "contact_first_name": contact_first_name,
        "rfq_one_liner": _rfq_one_liner(rfq),
        "preferred_process": rfq.get("product_category") or "manufacturing",
        "preferred_material": rfq.get("material") or "to be discussed",
        "target_quantity_phrase": _qty_phrase(rfq.get("quantity"), rfq.get("unit_of_measure")),
        "eau_phrase": _eau_phrase(rfq.get("quantity"), rfq.get("unit_of_measure"), bool(rfq.get("recurring"))),
        "key_constraint": _key_constraint(rfq),
        "required_certifications": certs_phrase,
        "email_followup_contact": buyer_email_spoken,
    }


@app.post("/api/call-vendor", response_model=CallResponse)
def call_vendor(req: CallVendorRequest) -> CallResponse:
    if not VAPI_ASSISTANT_ID:
        raise HTTPException(status_code=500, detail="VAPI_ASSISTANT_ID not set")
    sb = supabase_client()
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    rfq_resp = sb.table("rfqs").select("*").eq("id", req.rfq_id).limit(1).execute()
    if not rfq_resp.data:
        raise HTTPException(status_code=404, detail=f"rfq {req.rfq_id} not found")
    vendor_resp = sb.table("vendors").select("*").eq("id", req.vendor_id).limit(1).execute()
    if not vendor_resp.data:
        raise HTTPException(status_code=404, detail=f"vendor {req.vendor_id} not found")

    rfq = rfq_resp.data[0]
    vendor = vendor_resp.data[0]
    variables = _build_call_variables(rfq, vendor)
    metadata = {"rfq_id": req.rfq_id, "vendor_id": req.vendor_id}

    try:
        result = trigger_call(
            assistant_id=VAPI_ASSISTANT_ID,
            vendor_phone=HARDCODED_VENDOR_PHONE,
            variables=variables,
            metadata=metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vapi API error: {e}")

    call_id = result.get("id", "unknown")
    try:
        sb.table("vendors").update({"status": "calling"}).eq("id", req.vendor_id).execute()
    except Exception:
        pass
    return CallResponse(call_id=call_id)


@app.post("/api/call", response_model=CallResponse)
def make_call(req: CallRequest) -> CallResponse:
    variables = {
        "buyer_company": req.buyer_company,
        "buyer_one_liner": req.buyer_one_liner,
        "vendor_company": req.vendor_company,
        "contact_first_name": req.contact_first_name,
        "rfq_one_liner": req.rfq_one_liner,
        "preferred_process": req.preferred_process,
        "preferred_material": req.preferred_material,
        "target_quantity_phrase": req.target_quantity_phrase,
        "eau_phrase": req.eau_phrase,
        "key_constraint": req.key_constraint,
        "required_certifications": req.required_certifications,
        "email_followup_contact": req.email_followup_contact,
    }
    metadata: dict[str, Any] = {}
    if req.rfq_id:
        metadata["rfq_id"] = req.rfq_id
    if req.vendor_id:
        metadata["vendor_id"] = req.vendor_id
    if req.callback_url:
        metadata["callback_url"] = req.callback_url
    try:
        result = trigger_call(
            assistant_id=req.assistant_id,
            vendor_phone=HARDCODED_VENDOR_PHONE,
            variables=variables,
            metadata=metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vapi API error: {e}")
    return CallResponse(call_id=result.get("id", "unknown"))


@app.post("/vapi/webhook")
async def vapi_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    result = handle_webhook(payload)
    if result is None:
        return {"received": True}
    _persist_call_event(payload, result)
    callback_url = _extract_callback_url(payload)
    if callback_url:
        await _forward_to_callback(callback_url, result)
    return {"received": True, "event": result.get("event")}


def _persist_call_event(payload: dict[str, Any], result: dict[str, Any]) -> None:
    sb = supabase_client()
    if sb is None:
        return
    event = result.get("event")
    row = {
        "call_id": result.get("call_id"),
        "rfq_id": result.get("rfq_id"),
        "vendor_id": result.get("vendor_id"),
        "event_type": event,
        "status": result.get("status"),
        "payload": result,
        "raw": payload,
    }
    try:
        sb.table("call_events").insert(row).execute()
    except Exception:
        pass

    vendor_id = result.get("vendor_id")
    if not vendor_id:
        return
    if event == "status_update":
        status = result.get("status")
        if status:
            try:
                sb.table("vendors").update({"status": _map_call_status(status)}).eq("id", vendor_id).execute()
            except Exception:
                pass
    elif event == "call_complete":
        update: dict[str, Any] = {"status": "completed"}
        low = result.get("vendor_ballpark_unit_price_low")
        high = result.get("vendor_ballpark_unit_price_high")
        if low is not None and high is not None:
            update["unit_price"] = (float(low) + float(high)) / 2
        elif low is not None:
            update["unit_price"] = float(low)
        elif high is not None:
            update["unit_price"] = float(high)
        lead = result.get("vendor_lead_time_production_weeks") or result.get("vendor_lead_time_first_article_weeks")
        if lead is not None:
            try:
                update["lead_time"] = int(lead)
            except (ValueError, TypeError):
                pass
        if result.get("vendor_moq") is not None:
            try:
                update["moq"] = int(result["vendor_moq"])
            except (ValueError, TypeError):
                pass
        if result.get("outcome"):
            update["call_outcome"] = result["outcome"]
        if result.get("summary"):
            update["summary"] = result["summary"]
        if result.get("duration_seconds") is not None:
            update["call_duration"] = f"{int(result['duration_seconds'])}s"
        if result.get("transcript"):
            update["transcript"] = result["transcript"]
        if result.get("recording_url"):
            update["recording_url"] = result["recording_url"]
        if result.get("vendor_email_captured"):
            update["email"] = result["vendor_email_captured"]
        try:
            sb.table("vendors").update(update).eq("id", vendor_id).execute()
        except Exception:
            pass


def _map_call_status(vapi_status: str) -> str:
    # vapi: queued | ringing | in-progress | forwarding | ended
    if vapi_status in ("queued", "ringing", "in-progress", "forwarding"):
        return "calling"
    if vapi_status == "ended":
        return "completed"
    return "calling"


def _extract_callback_url(payload: dict[str, Any]) -> Optional[str]:
    msg = payload.get("message", payload)
    call = msg.get("call", {})
    metadata = call.get("metadata") or {}
    return metadata.get("callback_url")


async def _forward_to_callback(url: str, data: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=data)
    except Exception:
        pass


# GLD-21: dummy outreach data for demo. Only the first vendor gets a real call;
# the rest are populated with plausible synthetic state so the dashboard looks alive.
_DUMMY_STATUSES = ["responded", "qualified", "quoted", "no-response", "declined"]
_DUMMY_OUTCOMES = {
    "responded": "Interested, awaiting quote",
    "qualified": "Capability confirmed, moving to quote",
    "quoted": "Formal quote received",
    "no-response": "No callback after 2 attempts",
    "declined": "Capacity full this quarter",
}
_DUMMY_SUMMARIES = {
    "responded": "Spoke with sales lead — said they handle this volume regularly and will send a written quote within 48 hours.",
    "qualified": "Confirmed they have in-house tooling for the spec. Lead engineer to follow up with a tech callback to nail down tolerances.",
    "quoted": "Quote received: pricing competitive, lead time aligned. Open to negotiation on payment terms.",
    "no-response": "Two voicemails left, follow-up email sent. No callback yet — agent will retry mid-week.",
    "declined": "Politely declined: production line booked through Q3. Suggested checking back in 8 weeks.",
}


def _populate_dummy_vendor(row: dict, rfq: DiscoverVendorsRequest) -> dict:
    rng = random.Random(row["id"])  # deterministic per vendor id
    status = rng.choice(_DUMMY_STATUSES)
    contact = dict(row.get("contact") or {})
    if not contact.get("name"):
        contact["name"] = rng.choice(["Alex Chen", "Priya Shah", "Marco Rossi", "Sam Patel", "Rin Tanaka", "Dana Klein"])
    if not contact.get("title"):
        contact["title"] = rng.choice(["Sales Director", "Head of BD", "Procurement Lead", "Account Executive"])
    contact["email"] = _dummy_email_seeded(rng, row["name"])
    contact["phone"] = _dummy_phone_seeded(rng)
    row["contact"] = contact
    row["status"] = status
    row["email"] = contact["email"]
    if status in ("quoted", "qualified", "responded"):
        lo = rfq.budget_min or 20.0
        hi = rfq.budget_max or max(lo * 1.6, lo + 10)
        row["unit_price"] = round(rng.uniform(lo, hi), 2)
        row["lead_time"] = rng.choice([3, 4, 5, 6, 8, 10, 12]) if rfq.timeline_weeks is None else rng.randint(max(1, rfq.timeline_weeks - 2), rfq.timeline_weeks + 4)
        row["payment_terms"] = rng.choice(["net_30", "net_60", "advance"])
    if status != "no-response":
        row["call_duration"] = f"{rng.randint(120, 480)}s"
    row["call_outcome"] = _DUMMY_OUTCOMES[status]
    row["summary"] = _DUMMY_SUMMARIES[status]
    return row


def _dummy_email_seeded(rng: random.Random, name: str) -> str:
    base = "".join(c.lower() for c in name if c.isalnum())[:18] or "info"
    handle = rng.choice(["sales", "procurement", "info", "contact", "sourcing"])
    return f"{handle}@{base}.com"


def _dummy_phone_seeded(rng: random.Random) -> str:
    area = rng.choice(["415", "650", "408", "212", "312", "617"])
    return f"+1{area}{rng.randint(2000000, 9999999)}"


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
            for idx, c in enumerate(companies):
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
                # GLD-21: only the first vendor gets the real call. Others get
                # synthetic outreach state so the demo dashboard looks alive.
                if idx == 0:
                    # Pin the real test phone so the UI shows it and the call button dials it.
                    contact = dict(row.get("contact") or {})
                    contact["phone"] = HARDCODED_VENDOR_PHONE
                    row["contact"] = contact
                else:
                    _populate_dummy_vendor(row, req)
                sb.table("vendors").upsert(row).execute()
                vendors_out.append(row)

            # GLD-21: auto-trigger the real Vapi call on vendor #0 now that all rows are seeded.
            if vendors_out and VAPI_ASSISTANT_ID:
                vendor0 = vendors_out[0]
                try:
                    rfq_resp = sb.table("rfqs").select("*").eq("id", req.rfq_id).limit(1).execute()
                    rfq_row = rfq_resp.data[0] if rfq_resp.data else {}
                    variables = _build_call_variables(rfq_row, vendor0)
                    metadata = {"rfq_id": req.rfq_id, "vendor_id": vendor0["id"]}
                    trigger_call(
                        assistant_id=VAPI_ASSISTANT_ID,
                        vendor_phone=HARDCODED_VENDOR_PHONE,
                        variables=variables,
                        metadata=metadata,
                    )
                    sb.table("vendors").update({"status": "calling"}).eq("id", vendor0["id"]).execute()
                    vendors_out[0]["status"] = "calling"
                except Exception:
                    pass  # don't fail discovery if auto-call fails

            return {
                "vendors": vendors_out,
                "search_plan": {**plan, "headcount_range": list(headcount) if headcount else None},
            }
    except httpx.HTTPStatusError as e:
        return {"error": "upstream", "message": f"Crust Data {e.response.status_code}: {e.response.text[:200]}"}
    except Exception as e:
        return {"error": "upstream", "message": str(e)}
