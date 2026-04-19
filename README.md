# vendrsurf-backend

FastAPI backend for VendrSurf. Hosts Crust Data vendor discovery and (future) phone-call webhook handlers.

## Endpoints

- `POST /discover-vendors` — `{rfq_id, location, product_category, quantity?, budget_min?, budget_max?, timeline_weeks?}` → `{vendors, search_plan}`. Uses Claude (sonnet) to derive `{categories[], specialities[], title_keywords[]}` from the RFQ. Searches Crust Data companies by specialities then categories (HQ-country filter, headcount range derived from `quantity`, progressive fallback if <3 total). Person search filters by current-title keywords at company level; POCs ranked by title match + business-email availability. Upserts into Supabase `vendors`.

## Run locally

```
cp .env.example .env   # fill in keys
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy (Railway)

Uses `Procfile`. Set env vars: `ANTHROPIC_API_KEY`, `CRUST_DATA_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
