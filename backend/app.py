"""
QueryPilot backend — natural-language to SQL over PostgreSQL.

Flow:  POST /ask  ->  Gemini generates SQL from the question + live DB schema
               ->  SQL passes strict read-only validation
               ->  SQL executes with a statement timeout and a row cap
               ->  {question, sql, rows, columns} returned as JSON

MVP / IN PROGRESS: single-file backend, no auth, local dev only.
"""
import os
import re

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()  # reads ../.env when present; real secrets never live in code

# ---------------------------------------------------------------- config
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://querypilot:querypilot@localhost:5433/querypilot"
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
ROW_LIMIT = int(os.getenv("ROW_LIMIT", "200"))
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "10000"))

# ---------------------------------------------------------------- guardrails
# Keywords that can never appear in an accepted query (word-boundary, case-insensitive).
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|"
    r"VACUUM|ANALYZE|CLUSTER|REINDEX|LISTEN|NOTIFY|UNLISTEN)\b",
    re.IGNORECASE,
)
# A valid query must start with one of these after comments/whitespace are stripped.
ALLOWED_STARTERS = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)


def strip_sql_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments so keywords can't hide in them."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_readonly(sql: str) -> str:
    """
    Enforce read-only SQL. Raises ValueError with a clear reason if rejected,
    otherwise returns the cleaned single statement.
    Checks (in order): non-empty, single statement, allowed starter,
    no forbidden keywords anywhere (including inside CTEs/subqueries).
    """
    cleaned = strip_sql_comments(sql).strip()
    if not cleaned:
        raise ValueError("Empty SQL query.")
    # One statement only: allow a single trailing semicolon, reject stacked queries.
    body = cleaned[:-1].rstrip() if cleaned.endswith(";") else cleaned
    if ";" in body:
        raise ValueError("Multiple statements are not allowed.")
    if not ALLOWED_STARTERS.match(body):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if FORBIDDEN_KEYWORDS.search(body):
        raise ValueError("Query contains a forbidden write/DDL keyword.")
    return body


# ---------------------------------------------------------------- schema introspection
def load_schema() -> str:
    """Read tables + columns (+ FK relationships) from information_schema."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = [r[0] for r in cur.fetchall()]
        parts = []
        for t in tables:
            cur.execute(
                """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (t,),
            )
            cols = ", ".join(f"{c} ({d})" for c, d in cur.fetchall())
            parts.append(f"TABLE {t} ({cols})")
        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
            """
        )
        for src_t, src_c, dst_t, dst_c in cur.fetchall():
            parts.append(f"FOREIGN KEY {src_t}.{src_c} -> {dst_t}.{dst_c}")
        return "\n".join(parts)
    finally:
        conn.close()


# ---------------------------------------------------------------- Gemini SQL generation
def generate_sql(question: str, schema_text: str) -> str:
    """Ask Gemini to translate the question into a single SELECT query."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. See .env.example.")
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        GEMINI_MODEL,
        system_instruction=(
            "You translate natural-language questions into PostgreSQL SQL. "
            "Rules: output ONLY the SQL query, no markdown fences, no explanation, "
            "no trailing commentary. Only SELECT or WITH queries. "
            "Use only the tables and columns in the schema below. "
            "Prefer explicit JOINs over subqueries where readable."
        ),
    )
    prompt = f"Database schema:\n{schema_text}\n\nQuestion: {question}\n\nSQL:"
    resp = model.generate_content(prompt)
    sql = (resp.text or "").strip()
    # Strip accidental markdown fences the model may add despite instructions.
    sql = re.sub(r"^```(?:sql)?\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


# ---------------------------------------------------------------- query execution
def run_readonly_query(sql: str):
    """Execute validated SQL with a statement timeout and a hard row cap."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        batch = cur.fetchmany(ROW_LIMIT + 1)  # +1 to detect truncation
        truncated = len(batch) > ROW_LIMIT
        rows = batch[:ROW_LIMIT]
        return columns, rows, truncated
    finally:
        conn.close()


# ---------------------------------------------------------------- FastAPI app
app = FastAPI(title="QueryPilot", description="Natural-language SQL analytics (MVP)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local dev only; tighten for any deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

SCHEMA_TEXT = "(not loaded yet)"


@app.on_event("startup")
def _startup():
    global SCHEMA_TEXT
    SCHEMA_TEXT = load_schema()  # fail fast if the DB is unreachable


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)


@app.get("/health")
def health():
    return {"status": "ok", "gemini_configured": bool(GEMINI_API_KEY)}


@app.get("/schema")
def schema():
    return {"schema": SCHEMA_TEXT}


@app.post("/ask")
def ask(req: AskRequest):
    question = req.question.strip()
    try:
        raw_sql = generate_sql(question, SCHEMA_TEXT)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:  # Gemini API failure
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")
    try:
        sql = validate_readonly(raw_sql)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Rejected unsafe SQL ({e}). Generated SQL was: {raw_sql[:300]}",
        )
    try:
        columns, rows, truncated = run_readonly_query(sql)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SQL execution failed: {e}")
    return {
        "question": question,
        "sql": sql,
        "columns": columns,
        "rows": [list(r) for r in rows],  # FastAPI JSON-encodes dates/Decimals
        "truncated": truncated,
        "row_limit": ROW_LIMIT,
    }
