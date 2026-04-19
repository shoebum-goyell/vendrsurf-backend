"""
Vapi client for VendrSurf.

Three things in here:
1. build_assistant_config() — the full Vapi assistant payload
2. create_or_update_assistant() — one-time setup
3. trigger_call() — per-vendor outbound call with dynamic variables
4. handle_end_of_call_report() — process webhook event, return dashboard update

Requires: VAPI_API_KEY, VAPI_PHONE_NUMBER_ID env vars.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

from prompts import (
    SYSTEM_PROMPT,
    ANALYSIS_PROMPT,
    STRUCTURED_DATA_SCHEMA,
    SUCCESS_EVALUATION_PROMPT,
    FIRST_MESSAGE,
)


VAPI_BASE_URL = "https://api.vapi.ai"


# =============================================================================
# ASSISTANT CONFIG
# =============================================================================


def build_assistant_config(name: str = "VendrSurf Qualifier v1") -> dict[str, Any]:
    """
    Returns the full Vapi assistant configuration.

    Notes on the config choices:
    - gpt-4o for the model: good balance of reasoning + voice latency.
      If you want to test Claude Sonnet, swap provider/model below.
    - 11labs + Sarah voice: professional female voice, clear diction.
      Other good options: voiceId="jessica", voiceId="charlotte".
    - Deepgram Nova-3 transcriber: current default, good on technical vocab.
    - endCallPhrases + endCallFunctionEnabled: lets the model hang up cleanly.
    - silenceTimeoutSeconds=30: hang up if nothing is heard for 30s.
    - maxDurationSeconds=600: hard cap at 10 minutes.
    - backgroundSound="office": soft office ambience so silences don't feel dead.
    """

    server_url = os.environ.get(
        "WEBHOOK_URL",
        "https://vendrsurf-backend-production.up.railway.app/vapi/webhook",
    )

    return {
        "name": name,
        "serverUrl": server_url,
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
            ],
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "sarah",
            "stability": 0.5,
            "similarityBoost": 0.75,
            "speed": 1.0,
            "optimizeStreamingLatency": 3,
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en",
        },
        "firstMessage": FIRST_MESSAGE,
        "firstMessageMode": "assistant-speaks-first",
        "endCallFunctionEnabled": True,
        "endCallPhrases": [
            "have a good one",
            "thanks for the time",
            "talk to you soon",
        ],
        "endCallMessage": "Thanks, have a good one.",
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 600,
        "backgroundSound": "office",
        "backchannelingEnabled": False,
        "backgroundDenoisingEnabled": True,
        "analysisPlan": {
            "summaryPrompt": (
                "Summarize this hardware vendor qualification call in 2-3 "
                "sentences. Include the outcome, any pricing/lead time "
                "captured, and any objections or concerns raised."
            ),
            "structuredDataPrompt": ANALYSIS_PROMPT,
            "structuredDataSchema": STRUCTURED_DATA_SCHEMA,
            "successEvaluationPrompt": SUCCESS_EVALUATION_PROMPT,
            "successEvaluationRubric": "PassFail",
        },
    }


# =============================================================================
# CREATE / UPDATE ASSISTANT (one-time setup)
# =============================================================================


def _auth_headers() -> dict[str, str]:
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise RuntimeError("VAPI_API_KEY is not set")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def create_assistant() -> dict[str, Any]:
    """Create a new assistant. Returns the full assistant object including id."""
    config = build_assistant_config()
    resp = requests.post(
        f"{VAPI_BASE_URL}/assistant",
        headers=_auth_headers(),
        json=config,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_assistant(assistant_id: str) -> dict[str, Any]:
    """Update an existing assistant with the current config."""
    config = build_assistant_config()
    # PATCH takes the same fields, no wrapping needed
    resp = requests.patch(
        f"{VAPI_BASE_URL}/assistant/{assistant_id}",
        headers=_auth_headers(),
        json=config,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# =============================================================================
# TRIGGER OUTBOUND CALL (per vendor)
# =============================================================================


def trigger_call(
    assistant_id: str,
    vendor_phone: str,
    variables: dict[str, str],
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Trigger an outbound call.

    Args:
        assistant_id: The Vapi assistant id
        vendor_phone: Vendor contact number in E.164 format, e.g. "+14155551234"
        variables: Per-call dynamic variables. Must include all of:
            - buyer_company
            - buyer_one_liner
            - vendor_company
            - contact_first_name
            - rfq_one_liner
            - preferred_process
            - preferred_material
            - target_quantity_phrase
            - eau_phrase
            - key_constraint
            - required_certifications
            - email_followup_contact
        metadata: Anything you want echoed back on webhooks. Good for
            {"rfq_id": "...", "vendor_id": "..."} so you can tie the
            webhook back to dashboard rows.

    Returns:
        The Vapi call object including id. Store this id to correlate
        webhook events.
    """
    phone_number_id = os.environ.get("VAPI_PHONE_NUMBER_ID")
    if not phone_number_id:
        raise RuntimeError("VAPI_PHONE_NUMBER_ID is not set")

    required = [
        "buyer_company",
        "buyer_one_liner",
        "vendor_company",
        "contact_first_name",
        "rfq_one_liner",
        "preferred_process",
        "preferred_material",
        "target_quantity_phrase",
        "eau_phrase",
        "key_constraint",
        "required_certifications",
        "email_followup_contact",
    ]
    missing = [k for k in required if k not in variables]
    if missing:
        raise ValueError(f"Missing required variables: {missing}")

    payload: dict[str, Any] = {
        "assistantId": assistant_id,
        "phoneNumberId": phone_number_id,
        "customer": {"number": vendor_phone},
        "assistantOverrides": {
            "variableValues": variables,
        },
    }
    if metadata:
        payload["metadata"] = metadata

    resp = requests.post(
        f"{VAPI_BASE_URL}/call",
        headers=_auth_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# =============================================================================
# WEBHOOK HANDLER (what to do when Vapi pings you)
# =============================================================================


def handle_webhook(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Process a Vapi webhook event.

    Vapi fires many event types. The ones you care about for v1:
    - "end-of-call-report": the call finished. Contains transcript,
      recording URL, analysis (structuredData, summary, successEvaluation),
      and the metadata you passed when triggering the call.
    - "status-update": call state changed (optional, lets you show
      "ringing" / "in-progress" in the dashboard live).

    Returns a dashboard-ready dict if the event is actionable, else None.
    Your FastAPI route (or whatever) handles the persistence.
    """

    msg = event.get("message", event)  # Vapi wraps in .message sometimes
    msg_type = msg.get("type")

    if msg_type == "status-update":
        return _handle_status_update(msg)

    if msg_type == "end-of-call-report":
        return _handle_end_of_call_report(msg)

    # Other event types: transcript, hang, speech-update, etc.
    return None


def _handle_status_update(msg: dict[str, Any]) -> dict[str, Any]:
    call = msg.get("call", {})
    return {
        "event": "status_update",
        "call_id": call.get("id"),
        "status": msg.get("status"),  # "queued" | "ringing" | "in-progress" | "forwarding" | "ended"
        "rfq_id": (call.get("metadata") or {}).get("rfq_id"),
        "vendor_id": (call.get("metadata") or {}).get("vendor_id"),
    }


def _handle_end_of_call_report(msg: dict[str, Any]) -> dict[str, Any]:
    call = msg.get("call", {})
    analysis = msg.get("analysis", {})
    structured = analysis.get("structuredData", {}) or {}
    metadata = call.get("metadata") or {}

    return {
        "event": "call_complete",
        "call_id": call.get("id"),
        "rfq_id": metadata.get("rfq_id"),
        "vendor_id": metadata.get("vendor_id"),
        "ended_reason": msg.get("endedReason"),  # "customer-ended-call", etc.
        "duration_seconds": msg.get("durationSeconds"),
        "cost_usd": msg.get("cost"),
        "recording_url": msg.get("recordingUrl"),
        "transcript": msg.get("transcript"),
        "summary": analysis.get("summary"),
        "success": analysis.get("successEvaluation"),
        # Structured fields — flatten into the top-level update
        "outcome": structured.get("outcome"),
        "capability_qualified": structured.get("capability_qualified"),
        "capability_notes": structured.get("capability_notes"),
        "vendor_ballpark_unit_price_low": structured.get("vendor_ballpark_unit_price_low"),
        "vendor_ballpark_unit_price_high": structured.get("vendor_ballpark_unit_price_high"),
        "vendor_lead_time_first_article_weeks": structured.get("vendor_lead_time_first_article_weeks"),
        "vendor_lead_time_production_weeks": structured.get("vendor_lead_time_production_weeks"),
        "vendor_moq": structured.get("vendor_moq"),
        "vendor_nre_estimate_usd": structured.get("vendor_nre_estimate_usd"),
        "quote_interest_confirmed": structured.get("quote_interest_confirmed"),
        "quote_interest_reason": structured.get("quote_interest_reason"),
        "vendor_email_captured": structured.get("vendor_email_captured"),
        "vendor_requested_response_days": structured.get("vendor_requested_response_days"),
        "correct_contact_name": structured.get("correct_contact_name"),
        "correct_contact_title": structured.get("correct_contact_title"),
        "objections_raised": structured.get("objections_raised") or [],
        "vendor_notes_for_buyer": structured.get("vendor_notes_for_buyer"),
    }


# =============================================================================
# CLI — run this file directly to set up the assistant
# =============================================================================


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python vapi.py create                     # create new assistant\n"
            "  python vapi.py update <assistant_id>      # update existing assistant\n"
            "  python vapi.py test <assistant_id> <phone>  # fire a test call"
        )
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "create":
        result = create_assistant()
        print(f"Created assistant: {result['id']}")
        print(f"Save this ID and set VAPI_ASSISTANT_ID env var.")
        print(json.dumps(result, indent=2)[:500] + "...")

    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Need assistant_id")
            sys.exit(1)
        result = update_assistant(sys.argv[2])
        print(f"Updated assistant {result['id']}")

    elif cmd == "test":
        if len(sys.argv) < 4:
            print("Need assistant_id and phone (E.164 format, e.g. +14155551234)")
            sys.exit(1)
        # Example test call — edit variables for your actual RFQ
        test_vars = {
            "buyer_company": "Helios Robotics",
            "buyer_one_liner": (
                "Helios Robotics builds autonomous warehouse robots — "
                "headquartered in San Francisco, Series A."
            ),
            "vendor_company": "Precision CNC Inc",
            "contact_first_name": "Alex",
            "rfq_one_liner": "a mounting bracket for our robot chassis",
            "preferred_process": "CNC machining",
            "preferred_material": "6061 aluminum",
            "target_quantity_phrase": "five hundred units",
            "eau_phrase": "around ten thousand per year",
            "key_constraint": (
                "a bore tolerance of plus or minus two thousandths on the "
                "main mounting hole"
            ),
            "required_certifications": "ISO 9001",
            "email_followup_contact": "kaustubh at vendrsurf dot com",
        }
        result = trigger_call(
            assistant_id=sys.argv[2],
            vendor_phone=sys.argv[3],
            variables=test_vars,
            metadata={"rfq_id": "test-rfq-001", "vendor_id": "test-vendor-001"},
        )
        print(f"Call triggered: {result.get('id')}")
        print(json.dumps(result, indent=2)[:500] + "...")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
