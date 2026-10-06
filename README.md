# QueryPilot — natural-language SQL analytics

> **Status: IN PROGRESS / MVP.** Early working prototype, not a finished product. The core loop works: ask → SQL → results. Not built yet: auth, query history, charting, multi-database support. That's the roadmap, not the README.

Ask a question in plain English. Get an answer from your database.
Here's how it works. QueryPilot sends your question plus the live database schema to Google Gemini, which writes the SQL, and then every generated query passes strict read-only guardrails before it ever touches your database. Short version: nothing that writes gets through.

## How it works

```
question ──▶ Gemini (with live schema) ──▶ SQL ──▶ guardrails ──▶ Postgres ──▶ rows
```

**Guardrails (enforced in code, not cosmetic):**
- Only `SELECT` / `WITH` queries are accepted — validated after stripping SQL comments
- Forbidden keywords rejected anywhere in the query (including CTEs/subqueries):
  `INSERT UPDATE DELETE DROP ALTER CREATE TRUNCATE GRANT REVOKE COPY VACUUM …`
- Single statement only (stacked queries rejected)
- `statement_timeout` of 10s per query
- Results capped at 200 rows (truncation is reported)

## Quickstart

**1. Start the demo database** (PostgreSQL 16, seeded with a small e-commerce
schema: `customers`, `products`, `orders`):

```bash
cd db
docker compose up -d
```

**2. Configure:**

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY (get one at https://aistudio.google.com/app/apikey)
```

**3. Install + run the API:**

```bash
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 8000
```

**4. Open the frontend:** just open `frontend/index.html` in a browser
(or serve it: `python3 -m http.server --directory frontend 8080`).

Try: *"Top 5 products by revenue"*, *"Which city has the most customers?"*,
*"Monthly order counts for 2026"*.

## API

| Method | Path      | Description                                    |
|--------|-----------|------------------------------------------------|
| GET    | /health   | liveness + whether Gemini is configured        |
| GET    | /schema   | the introspected schema injected into prompts  |
| POST   | /ask      | `{question}` → `{question, sql, columns, rows, truncated}` |

## Project layout

```
querypilot/
├── backend/app.py        # FastAPI: /ask, Gemini SQL gen, guardrails, schema introspection
├── db/
│   ├── docker-compose.yml# PostgreSQL 16 (host port 5433)
│   └── init.sql          # seed: customers / products / orders (~100 rows)
├── frontend/index.html   # single-file dark UI, no build step
├── requirements.txt
├── .env.example
└── README.md
```

## Roadmap (not built yet)

- [ ] Per-user query history
- [ ] Result charting
- [ ] Multiple saved database connections
- [ ] Streaming SQL generation
- [ ] Auth
