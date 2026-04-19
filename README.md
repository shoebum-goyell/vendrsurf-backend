# vendrsurf-backend

FastAPI backend for VendrSurf. Hosts Crust Data vendor discovery and (future) phone-call webhook handlers.

## Endpoints

- `POST /discover-vendors` — `{rfq_id, location, product_category, quantity?, budget_min?, budget_max?, timeline_weeks?}` → `{vendors, search_plan}`. Uses Claude (haiku) to derive Crust Data company keywords + procurement role patterns from the RFQ, searches Crust Data companies per-keyword (HQ-country filter, global fallback if <3 total), finds POCs whose titles match the role patterns, upserts into Supabase `vendors`.

## Run locally

```
cp .env.example .env   # fill in keys
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy (Railway)

Uses `Procfile`. Set env vars: `ANTHROPIC_API_KEY`, `CRUST_DATA_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
