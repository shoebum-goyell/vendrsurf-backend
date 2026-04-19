# VendrSurf Backend

FastAPI backend for [VendrSurf](https://vendrsurf.vercel.app) — AI-powered hardware procurement.

Handles vendor discovery (Claude + Crust Data), outbound calling (Vapi), and post-call data extraction. Deployed on Railway.

## Endpoints

### `POST /discover-vendors`
```json
{ "rfq_id": "...", "location": "US", "product_category": "CNC machining", "quantity": 500 }
```
Claude derives search intent (categories, specialities, title keywords) from the RFQ. Searches Crust Data for matching companies + decision-maker contacts. Ranks by title match + business-email availability. Upserts into Supabase `vendors`. Auto-fires a Vapi call on vendor #0; vendors 1–9 get deterministic demo statuses.

### `POST /parse-rfq`
```json
{ "transcript": "We need 500 aluminum brackets..." }
```
Claude (Sonnet) extracts 14 structured RFQ fields from a voice transcript: product description, category, quantity, pricing, certifications, timeline, and more.

### `POST /api/call-vendor`
```json
{ "rfq_id": "...", "vendor_id": "..." }
```
Maps RFQ + vendor rows to Vapi call variables and triggers an outbound call. Phone hardcoded for demo.

### `POST /vapi/webhook`
Receives Vapi `end-of-call-report` events. Persists transcript, recording URL, and structured analysis (unit price, lead time, MOQ, NRE) to Supabase `call_events` and `vendors`.

## Stack

- FastAPI + uvicorn
- Claude Sonnet (Anthropic) — RFQ parsing + vendor search planning
- Crust Data API — company + person enrichment
- Vapi — outbound voice calling + post-call analysis
- Supabase (service role) — DB writes

## Run locally

```bash
cp .env.example .env   # fill in keys
pip install -r requirements.txt
uvicorn main:app --reload
```

## Env vars

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `CRUST_DATA_API_KEY` | Vendor enrichment |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role (write access) |
| `VAPI_API_KEY` | Vapi outbound calling |
| `VAPI_PHONE_NUMBER_ID` | Vapi phone number |
| `VAPI_ASSISTANT_ID` | Pre-configured Vapi assistant |
| `WEBHOOK_URL` | Public URL for Vapi to post webhooks |

## Deploy

Uses `Procfile`. Deployed on Railway — push to `main` auto-deploys.
