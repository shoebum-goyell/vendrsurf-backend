# vendrsurf-backend

FastAPI backend for VendrSurf. Hosts Crust Data vendor discovery and (future) phone-call webhook handlers.

## Endpoints

- `POST /discover-vendors` — `{rfq_id, location, product_category}` → `{vendors}`. Searches Crust Data companies (HQ-country filter, global fallback if <3), finds procurement POCs, upserts into Supabase `vendors`.

## Run locally

```
cp .env.example .env   # fill in keys
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy (Railway)

Uses `Procfile`. Set env vars: `CRUST_DATA_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
