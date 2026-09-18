# PRD — HR AI Suite ("PeopleOps Copilot")

**Status:** Draft v2 · **Date:** 2026-09-18 · **Build budget:** 60 minutes, multi-agent parallel execution
**Stack:** React (Vite) + FastAPI + Postgres/pgvector + Pydantic AI · fully containerized · **OpenRouter free tier** (Groq fallback, Ollama offline)

---

## 1. Summary & Problem

HR teams answer the same policy questions hundreds of times a month and screen resumes by eyeball. Two costs: employees wait days for a leave/reimbursement answer that is written down in a PDF nobody reads, and recruiters spend ~15 min per resume producing an inconsistent, unauditable judgement.

**PeopleOps Copilot** is a containerized full-stack web application with two anchor AI features and four supporting ones:

1. **Policy RAG** — grounded Q&A over the company's own HR policy corpus, with inline citations back to document + section.
2. **Resume Evaluator Agent** — an agentic, tool-using evaluator that scores a resume against a specific Job Description on a fixed rubric, produces evidence-linked reasoning, and emits a structured verdict.

Everything else exists to make those two demonstrable end to end.

**Two constraints drive every decision below:** it must be buildable in one hour by parallel coding agents, and it must run on **free LLM inference** — no paid API key anywhere on the critical path.

---

## 2. Goals, Non-Goals, Constraints

### Goals

| # | Goal | Measured by |
|---|---|---|
| G1 | Grounded policy answers with citations | ≥90% of answers cite ≥1 real chunk; zero uncited factual claims in demo set |
| G2 | Resume↔JD evaluation a recruiter would sign | Structured score + per-criterion evidence quotes for 5/5 sample resumes |
| G3 | Ship a working full-stack app in 60 min | `docker compose up` → all 6 features clickable, no stubs on the happy path |
| G4 | Zero LLM spend | Runs end to end on a free-tier key, or fully offline via Ollama |
| G5 | Parallel-safe codebase | 9 agents, zero merge conflicts, because file ownership is disjoint |

### Non-Goals (v1)

- Auth / multi-tenancy / RBAC. Single hardcoded demo org, `X-Demo-User` header only.
- ATS integration, email, calendar, offer letters, payroll.
- Kubernetes, CI/CD, TLS, production secrets management.
- Fine-tuning, GPU serving, eval harness beyond the smoke set.

### Hard Constraints

- **C1 — 60 minutes wall clock.** Everything must come from a pulled image or a pip/npm install. No manual provisioning.
- **C2 — Two containers of app code, not five services.** One FastAPI backend, one React frontend. A microservice split doubles the integration surface, and integration is what kills parallel builds.
- **C3 — Contract freeze at T+10.** After minute 10, `backend/app/schemas.py`, `frontend/src/types.ts`, and `db/init.sql` are immutable.
- **C4 — No agent touches a file another agent owns.** Enforced by the ownership table in §11.
- **C5 — Free-tier rate limits are a design input, not a surprise.** ~30 requests/min shapes the concurrency pool (§10).

---

## 3. Personas & Journeys

| Persona | Name | Needs | Primary feature |
|---|---|---|---|
| Employee | Asha, engineer | "How many sick days carry over?" answered in 10s, with proof | F1 Policy Chat |
| Recruiter | Ravi, talent acquisition | Screen 40 resumes against a JD tonight, defensibly | F2 Evaluator, F5 Ranking Board |
| HR Manager | Meera, HRBP | Write a JD fast; see where the funnel leaks | F3 JD Studio, F6 Analytics |
| Interviewer | Dev, tech lead | Questions targeted at *this* candidate's actual gaps | F4 Interview Kit |

### Journey A — Policy answer (Asha)

`/chat` → type question → answer streams in → 1–3 citation chips render → click a chip → right drawer opens the exact source chunk, highlighted, with document title and section heading.

### Journey B — Screening run (Ravi)

`/jobs/{id}` → drop 5 resume PDFs → each row goes `queued → parsing → evaluating → scored` live over SSE → board sorts by fit score → click a row → rubric table, per-criterion evidence quote pulled verbatim from the resume, gaps list, recommendation, and a "Generate interview kit" button that hands off to F4.

---

## 4. Feature Specs

One feature, one owning agent. The acceptance criterion *is* that agent's definition of done.

### F1 — Policy RAG Chat *(anchor)*

- **Ingestion:** admin uploads PDF/MD/TXT at `/admin`; backend parses → section-aware chunks → embeddings → pgvector.
- **Retrieval:** hybrid. pgvector cosine top-20 ∪ Postgres FTS (`ts_rank_cd`) top-20 → Reciprocal Rank Fusion → top-8 → LLM rerank to top-5.
- **Generation:** answers **only** from retrieved context. The refusal path is mandatory: if no chunk supports the question, respond *"That isn't covered in the policies I have — here's who to ask."* Never a guess.
- Every factual sentence carries a citation id resolving to `chunk_id → document + section + char range`.
- Multi-turn: last 6 turns kept; follow-ups are query-rewritten to standalone before retrieval.

**Acceptance:** 10 seeded questions → 10 grounded answers with working citation chips; 2 out-of-corpus questions → both refuse cleanly.

### F2 — Resume Evaluator Agent *(anchor)*

An **agentic** evaluator, not a single prompt. A Pydantic AI agent runs a tool loop:

| Tool | Purpose |
|---|---|
| `search_policy(query)` | Check internal hiring-bar / role-level policy (reuses F1 retrieval) |
| `get_jd(job_id)` | Fetch parsed JD: must-haves, nice-to-haves, min years, level |
| `get_resume_section(resume_id, section)` | Pull one section at a time — experience / education / skills / projects — so the model reads deliberately instead of swallowing the whole document |

**Request-budget note:** an earlier draft gave the agent a `score_criterion` tool called once per rubric row — 7 extra round trips per resume. Against OpenRouter's 50-requests/day free cap that is one resume per ~8% of the daily budget. Scoring is therefore **not a tool**: all seven criteria come back in the single typed final output. Tools are reserved for *fetching* what the model cannot already see.

Loop ends when the agent emits a typed `EvaluationResult` (Pydantic model, validated + auto-retried on schema failure). Budget: **3–4 requests per resume** (1–2 tool-fetch turns + 1 final + ≤1 validation retry).

Rubric (weights configurable per job, defaults shown):

| Criterion | Weight |
|---|---|
| Must-have skills coverage | 30% |
| Relevant years of experience | 20% |
| Domain / industry fit | 15% |
| Seniority & scope of ownership | 15% |
| Project & impact evidence | 10% |
| Education / certifications | 5% |
| Tenure stability | 5% |

Each criterion: `score 0–10`, `evidence_quote` (verbatim, or `null` with `"not evidenced"`), `reasoning` (≤2 sentences). Weighted sum → `fit_score 0–100` → recommendation band.

**Bias guard — ships in v1, non-negotiable:** before evaluation, the parsed resume goes through a redaction step stripping name, email, phone, address, photo refs, gender markers, DOB, nationality, and marital status; university *prestige* signals are reduced to degree + field. The agent only ever sees the redacted view. The UI shows the human the unredacted resume and badges the evaluation *"scored on redacted text."* Every evaluation writes an immutable `audit_log` row: model id, provider, prompt version, rubric version, full tool trace.

**Acceptance:** 5 sample resumes × 1 JD → 5 evaluations, each ≥6 criteria scored, every non-null evidence quote present in the redacted resume text (string-match assertion in the smoke test).

### F3 — JD Studio

Input: role title, level, must-haves, nice-to-haves, location, comp band. Output: a full JD (summary, responsibilities, requirements, benefits) generated as a typed Pydantic model so it parses straight into the `jobs` table. Includes an **inclusive-language pass** flagging gendered/ageist/exclusionary phrasing with suggested rewrites. The saved JD becomes F2's input.

**Acceptance:** 5-field form → JD created → appears in job list → F2 can evaluate against it.

### F4 — Interview Kit Generator

Takes an F2 evaluation and generates a candidate-specific kit: 6–8 questions aimed at the *gaps* the evaluator found, each with what-good-looks-like, a follow-up probe, and a scoring anchor. Plus 2 questions validating claimed strengths.

**Acceptance:** one click from any evaluation detail page → kit whose questions reference ≥2 named gaps from that evaluation.

### F5 — Candidate Ranking Board

Per-job table of evaluated candidates: fit score, recommendation, top-3 matched skills, top-3 gaps. Sort, filter by band, side-by-side compare of any two. Bulk upload drops N resumes and evaluates them through a **bounded concurrency pool sized to the provider's rate limit** (default 2 on OpenRouter). Live status via SSE.

**Acceptance:** upload 5 resumes at once → all 5 reach `scored` → board sorts correctly by fit score.

### F6 — HR Analytics

Dashboard over local data: pipeline counts by stage, fit-score distribution histogram, top-10 skills present vs. top-10 missing across the pool, average evaluation latency and token count. Plus a natural-language query box that emits a *parameterized* read-only query via typed output against an **allowlist of tables and columns** — no free-form SQL reaches the database, and the DB role used for this path is `SELECT`-only.

**Acceptance:** 4 live charts rendering from evaluation data produced during the run.

---

## 5. Free LLM Providers — The Decision

All four hosted options below are **OpenAI-compatible**, so they sit behind one client and swap with a single env var. Verified September 2026; free-tier numbers move, so the app logs which provider and model answered each request.

| Provider | Free tier | Card needed | Base URL | Default model |
|---|---|---|---|---|
| **OpenRouter** *(default)* | 20 RPM · **50 free-model req/day**, rising to 1,000/day after a one-time $10 credit purchase | No | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct:free` |
| **Groq** | 30 RPM · 1,000 req/day · 12K TPM · 100K TPD on `llama-3.3-70b-versatile` | No | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| **Google AI Studio** | Large context (up to 1M), lower request volume; limits vary by project | No | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| **Cerebras** | ~1M tokens/day, very fast — **but became card-required in July 2026** | **Yes** | `https://api.cerebras.ai/v1` | `llama-3.3-70b` |
| **Ollama** *(offline)* | Unlimited, local, no key, no network | No | `http://ollama:11434/v1` | `qwen2.5:7b-instruct` |

**Choice: OpenRouter as default** — one key reaches the widest catalog of free models, so swapping `llama-3.3-70b-instruct:free` for `deepseek-chat-v3:free` or `qwen-2.5-72b-instruct:free` is a config change, not a new signup. **Groq is the configured fallback** (`LLM_FALLBACK_PROVIDER=groq`) and **Ollama is the offline escape hatch**. Cerebras is documented but not default — it now requires a card, which violates G4.

OpenRouter specifics the code must handle:
- Send `HTTP-Referer` and `X-Title` headers. OpenRouter uses them for free-tier attribution, and requests without them are deprioritized.
- **Not every free model supports tool calling.** F2's agent needs it. `meta-llama/llama-3.3-70b-instruct:free` and `qwen/qwen-2.5-72b-instruct:free` do; several other `:free` models do not. Startup runs a one-shot tool-calling probe against the configured model and logs a loud warning if it fails — better to learn this at T+10 than at T+45.
- Free models are served best-effort and can return 502/503 under load. Retry policy treats those exactly like 429.

**Three things to be honest about:**
- **50 requests/day is the binding constraint, and it is tight.** It is the single most important number in this document. §10 explains the design change it forces.
- **Buying $10 of credit once lifts the cap to 1,000/day** and is the difference between a comfortable build hour and a frustrating one. It is not required — the design fits inside 50/day — but it is the cheapest risk reduction available here.
- **Free tiers generally train on your prompts.** Fine for synthetic seed data and a demo. Not fine for real resumes or real employee questions. For anything real, run the Ollama profile — the only configuration in this repo where no data leaves the machine. Called out in the README and in the `/admin` UI.

### Embeddings — free, and local by default

| Option | Cost | Notes |
|---|---|---|
| **FastEmbed** (`BAAI/bge-small-en-v1.5`, 384-dim) *(default)* | Free, offline, in-process ONNX | ~130MB model baked into the backend image. No key, no rate limit, no network. Removes an entire failure mode from the build |
| Google Gemini Embedding | Free: 1,500 req/day, no card | Swap-in if better recall is needed |
| Jina Embeddings v4 | Free: 1M tokens/month | Multimodal (text + PDF) if resume layout matters later |

**Choice: FastEmbed local.** In a 60-minute containerized build, an embedding provider that cannot rate-limit, cannot 401, and cannot be down is worth more than a few points of retrieval quality. `EMBEDDING_PROVIDER=gemini|jina` is wired but not the default.

---

## 6. Architecture

```
                         ┌─────────────────────────────────┐
  browser  ──────────────►  frontend  (React 19 + Vite)    │
                         │  nginx :80  ·  /api → backend   │
                         └────────────────┬────────────────┘
                                          │ REST + SSE
                         ┌────────────────▼────────────────┐
                         │  backend  (FastAPI, uvicorn)    │  :8000
                         │                                 │
                         │  routers/   policies chat jobs  │
                         │             resumes evaluate    │
                         │             interview_kit       │
                         │             analytics           │
                         │  rag/       chunk embed retrieve│
                         │  agents/    evaluator jd kit    │  ← Pydantic AI
                         │  llm/       provider throttle   │  ← OpenAI-compatible
                         │  redact.py  PII stripping       │
                         └───────┬──────────────────┬──────┘
                                 │                  │
              ┌──────────────────▼────┐   ┌─────────▼──────────────────┐
              │  db  pgvector/pg17    │   │  free LLM provider          │
              │  relational + vector  │   │  Groq (default)             │
              │  + Postgres FTS       │   │  ├ Gemini    fallback       │
              │  :5432  volume: pgdata│   │  ├ OpenRouter               │
              └───────────────────────┘   │  └ ollama:11434  (profile)  │
                                          └─────────────────────────────┘
```

**Why one database container:** pgvector gives vector search, relational storage, and BM25-ish full-text ranking in a single image. A separate Qdrant would mean two stores to keep in sync, two health checks, and two ways for the demo to half-work.

**Data flow — RAG:** upload → parse → section-aware chunk (~800 tok, 100 overlap, never split a numbered clause) → FastEmbed batch → `chunks.embedding` + `tsvector` → query: rewrite → vector ‖ FTS → RRF → LLM rerank → grounded answer with citations.

**Data flow — Evaluation:** upload → parse → redact → section-split → Pydantic AI tool loop (throttled) → validated `EvaluationResult` → persist + audit row → SSE pushes status to the board.

---

## 7. Tech Stack

| Layer | Choice | Why, given 60 minutes and parallel agents |
|---|---|---|
| Frontend | React 19 + Vite + TypeScript | Vite dev server starts in <1s; HMR survives container rebuilds via bind mount |
| Routing / data | React Router 7 + TanStack Query | Query kills the hand-rolled loading/error state each agent would otherwise invent differently |
| Styling | Tailwind + shadcn/ui | Agents produce visually consistent UI with no design round-trip |
| Charts | Recharts | Declarative; F6 ships charts without fighting canvas |
| Backend | FastAPI + Pydantic v2 + uvicorn | Pydantic models double as the API contract *and* the LLM output schema — one definition, two jobs |
| ORM | SQLAlchemy 2.0 (async) + asyncpg | Async all the way through so 3 concurrent evaluations don't block the event loop |
| Migrations | Plain `db/init.sql` | Alembic is the right call for a real project and the wrong call for a 60-minute build |
| **LLM framework** | **Pydantic AI** | Provider-agnostic over any OpenAI-compatible endpoint; typed structured output with automatic validation-retry; built-in tool loop with dependency injection. Gives us F2's agent without hand-writing a `while tool_calls:` loop |
| LLM transport | `openai` SDK w/ `base_url` override | Every free provider in §5 speaks this protocol. Switching providers is one env var |
| Embeddings | FastEmbed (ONNX, in-process) | No key, no rate limit, no network call |
| PDF parsing | `pypdf` | Pure Python, no native build step in the image |
| Vector store | pgvector `HNSW` index | Same container as the relational data |
| Container | Docker + Compose, multi-stage builds | §12 |

---

## 8. Data Model

`db/init.sql` — applied automatically by the postgres image on first boot.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE documents (
  id UUID PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
  source_name TEXT, char_count INT, uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE chunks (
  id UUID PRIMARY KEY,
  document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
  section TEXT, ordinal INT, text TEXT NOT NULL,
  start_char INT, end_char INT, token_count INT,
  embedding vector(384),                                   -- bge-small-en-v1.5
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks USING gin (tsv);

CREATE TABLE jobs (
  id UUID PRIMARY KEY, title TEXT NOT NULL, level TEXT, location TEXT,
  department TEXT, description_md TEXT,
  must_haves JSONB DEFAULT '[]', nice_to_haves JSONB DEFAULT '[]',
  min_years INT, comp_min INT, comp_max INT,
  rubric_weights JSONB, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE resumes (
  id UUID PRIMARY KEY, candidate_label TEXT NOT NULL,      -- "Candidate A"
  file_name TEXT, raw_text TEXT, redacted_text TEXT,
  sections JSONB DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',                   -- queued|parsing|evaluating|scored|failed
  error TEXT, uploaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE evaluations (
  id UUID PRIMARY KEY,
  resume_id UUID REFERENCES resumes(id) ON DELETE CASCADE,
  job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
  fit_score INT, recommendation TEXT,                      -- strong_yes|yes|maybe|no
  criteria JSONB, matched_skills JSONB, gaps JSONB, summary TEXT,
  provider TEXT, model TEXT, rubric_version TEXT, prompt_version TEXT,
  input_tokens INT, output_tokens INT, latency_ms INT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (resume_id, job_id)
);

CREATE TABLE interview_kits (
  id UUID PRIMARY KEY,
  evaluation_id UUID REFERENCES evaluations(id) ON DELETE CASCADE,
  questions JSONB, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE audit_log (
  id UUID PRIMARY KEY, entity_type TEXT, entity_id UUID, action TEXT,
  actor TEXT, tool_trace JSONB, payload JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- F6 NL→SQL runs as this role, which can only read.
CREATE ROLE analytics_ro NOLOGIN;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro;
```

`candidate_label` is a generated pseudonym. Real identity lives only in `resumes.raw_text` and never reaches the model.

---

## 9. API Contract — **FROZEN AT T+10**

Pydantic models in `backend/app/schemas.py` are the source of truth; `frontend/src/types.ts` mirrors them exactly. Nobody edits either after minute 10 without a broadcast to all running agents.

```python
# backend/app/schemas.py  — owned by A0, read-only to everyone else
Recommendation = Literal["strong_yes", "yes", "maybe", "no"]
ResumeStatus   = Literal["queued", "parsing", "evaluating", "scored", "failed"]

class Citation(BaseModel):
    chunk_id: UUID; document_title: str; section: str
    quote: str; start_char: int; end_char: int

class ChatRequest(BaseModel):
    question: str
    history: list[Turn] = []

class ChatResponse(BaseModel):
    answer: str; citations: list[Citation]; grounded: bool
    provider: str; model: str

class CriterionScore(BaseModel):
    criterion: str
    score: int = Field(ge=0, le=10)
    weight: float
    evidence_quote: str | None          # verbatim from redacted resume, or None
    reasoning: str = Field(max_length=300)

class EvaluationResult(BaseModel):
    id: UUID; resume_id: UUID; job_id: UUID
    fit_score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    criteria: list[CriterionScore]
    matched_skills: list[str]; gaps: list[str]; summary: str
    meta: EvalMeta                       # provider, model, latency_ms, tokens

class InterviewQuestion(BaseModel):
    question: str; targets: str          # which gap/strength it probes
    good_answer: str; follow_up: str; scoring_anchor: str
```

| Method | Route | Body → Response |
|---|---|---|
| GET | `/api/health` | → `{status, db, llm_provider, llm_reachable}` |
| POST | `/api/policies` | multipart file → `{document_id, chunk_count}` |
| GET | `/api/policies` | → `{documents: [Document]}` |
| POST | `/api/chat` | `ChatRequest` → `ChatResponse` |
| GET | `/api/chat/stream` | SSE token stream (optional; REST is the contract) |
| POST | `/api/jobs` | `JobDraft` → `{job: Job}` (generates the JD) |
| GET | `/api/jobs` · `/api/jobs/{id}` | → `{jobs}` · `{job, evaluations}` |
| POST | `/api/resumes` | multipart `files[]` + `job_id` → `{resumes: [{id, status}]}` |
| GET | `/api/resumes/stream?job_id=` | SSE status updates for the board |
| POST | `/api/evaluate` | `{resume_id, job_id}` → `EvaluationResult` |
| GET | `/api/evaluations/{id}` | → `EvaluationResult` |
| POST | `/api/interview-kit` | `{evaluation_id}` → `{questions: [InterviewQuestion]}` |
| GET | `/api/analytics` | → `{pipeline, score_distribution, skill_gaps, usage}` |
| POST | `/api/analytics/query` | `{question}` → `{sql, rows, explanation}` |

**All errors:** `{"error": {"code": str, "message": str}}` with a real HTTP status. Rate-limit exhaustion is its own code — `llm_rate_limited`, HTTP 429, with `retry_after` — so the UI can say *"free tier limit hit, retrying in Ns"* instead of showing a generic failure.

---

## 10. AI Layer Spec

### Provider abstraction

```python
# backend/app/llm/provider.py   (owned by A0)
PROVIDERS = {
  "openrouter": Provider("https://openrouter.ai/api/v1",                           "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free",
                         headers={"HTTP-Referer": APP_URL, "X-Title": "PeopleOps Copilot"}),
  "groq":       Provider("https://api.groq.com/openai/v1",                         "GROQ_API_KEY",       "llama-3.3-70b-versatile"),
  "gemini":     Provider("https://generativelanguage.googleapis.com/v1beta/openai/","GEMINI_API_KEY",    "gemini-2.0-flash"),
  "cerebras":   Provider("https://api.cerebras.ai/v1",                             "CEREBRAS_API_KEY",   "llama-3.3-70b"),
  "ollama":     Provider("http://ollama:11434/v1",                                  None,                "qwen2.5:7b-instruct"),
}
```

One `get_agent(output_type, tools, system_prompt)` factory returns a configured Pydantic AI `Agent`. Feature agents never construct a client themselves, never hardcode a model name, and never add headers.

### Request budget — the design driver

OpenRouter's free tier allows **50 model requests per day** before any credit purchase. Every LLM call in this app is spent against that. Budget for a full demo:

| Path | Requests | Notes |
|---|---|---|
| Startup tool-calling probe | 1 | Once per boot |
| Policy question (F1) | 2 | query rewrite + answer. **Rerank is disabled by default** (`RAG_RERANK=false`) — it cost a third of every question's budget for a marginal recall gain |
| Resume evaluation (F2) | 3–4 | 1–2 tool-fetch turns + typed final + ≤1 retry |
| JD generation (F3) | 1 | Single typed call, inclusive-language pass folded into the same output |
| Interview kit (F4) | 1 | |
| NL→SQL (F6) | 1 | |

Demo total ≈ **2 policy questions (4) + 5 evaluations (20) + 1 JD (1) + 1 kit (1) + 1 query (1) = 27 requests.** Fits inside 50 with headroom for two retries and a rehearsal — but only just, which is why the next two mechanisms are mandatory rather than nice-to-have.

**Seed data is pre-evaluated and committed.** `make seed` loads evaluations from `seed/evaluations.json` rather than generating them, so the board and the analytics dashboard are populated with **zero** request spend. Live generation during the demo is for the resumes the presenter uploads on stage, not for the baseline.

**A daily request counter is enforced server-side.** `audit_log` is counted per UTC day; at `LLM_DAILY_BUDGET` (default 45) the API returns `llm_budget_exhausted` with a clear message instead of silently failing at the provider. The `/admin` page shows requests used today. Running out of budget mid-demo with no warning is the worst failure mode in this project; a counter costs 10 lines.

### Throttle & fallback

A shared async token bucket (`LLM_RPM`, default **18** — under OpenRouter's 20) wraps every call; `EVAL_CONCURRENCY` defaults to **2**. Retries treat 429, 502 and 503 identically — free models are best-effort and 5xx under load is normal, not exceptional. Policy: exponential backoff with jitter, 3 attempts, then failover to `LLM_FALLBACK_PROVIDER` (default `groq`) if its key is set, then a typed `llm_rate_limited` error carrying `retry_after`. Every call logs `provider · model · latency · tokens` into `audit_log`, which feeds both the daily counter and F6's usage chart.

### Chunking (F1)

Split on markdown headings and numbered clauses first, then pack to ~800 tokens with 100-token overlap. **Never split mid-clause** — half a policy sentence produces a citation that misleads, which is worse than no answer. The section heading is prefixed into the chunk text so retrieval sees the context.

### Retrieval (F1)

```
rewrite(question, history) -> standalone query
vec  = SELECT ... ORDER BY embedding <=> $1 LIMIT 20
fts  = SELECT ... ORDER BY ts_rank_cd(tsv, plainto_tsquery($1)) DESC LIMIT 20
fused = RRF(vec, fts, k=60)[:8]
final = llm_rerank(query, fused)[:5]
```

### Generation prompt contract (F1)

System prompt: answer only from `<context>`; cite `[^chunk_id]` after every factual sentence; if context is insufficient, say so and name the HR contact; never fill gaps from general knowledge. Context blocks carry their `chunk_id` so citations resolve deterministically.

Small-model reality: on a 70B free model, the citation format holds only if it's demonstrated. The system prompt carries **two worked examples** — one grounded answer, one refusal — and the response is validated as a Pydantic model so a malformed answer triggers Pydantic AI's automatic retry rather than reaching the user.

### Evaluator agent (F2)

Pydantic AI agent, tools per §4, `output_type=EvaluationResult`, `retries=2`. The JD and rubric go in the system prompt (stable across all N candidates for a job); the redacted resume is the user message.

**Guardrails in the prompt, not just in review:** never infer protected attributes; never score on name, school prestige, employment gaps, or photo; if a criterion has no supporting text, score it low with `evidence_quote=None` and say "not evidenced" — never fabricate a quote. Hard cap of 6 tool calls; on cap, emit a partial result with unscored criteria marked `not evidenced`.

### Verification — the cheap hallucination check

After every evaluation: each non-null `evidence_quote` must be a substring of `resumes.redacted_text` (normalized whitespace). Failures mark the criterion `unverified` in the UI and fail the smoke test. Deterministic, costs nothing, and catches the single most damaging failure mode — a fabricated quote attributed to a real candidate.

---

## 11. Parallel Agent Build Plan

The core idea: **parallelism is bought with contracts, not with coordination.** Agents don't talk to each other. They talk to `schemas.py`, `types.ts`, and `init.sql` — all of which exist before any feature agent starts.

### Agent roster

| # | Agent | Owns (exclusive write access) | Wave |
|---|---|---|---|
| A0 | **Contract** | `backend/app/schemas.py`, `db.py`, `db/init.sql`, `llm/provider.py`, `llm/throttle.py`, `frontend/src/types.ts`, `frontend/src/api.ts`, `.env.example` | 0 |
| A1 | **Infra** | `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, `requirements.txt`, `package.json`, `vite.config.ts`, `Makefile`, `README.md` | 0 |
| A2 | **Scaffold** | `backend/app/main.py`, `frontend/src/App.tsx`, `main.tsx`, `index.css`, `components/Shell.tsx` | 0 |
| A3 | **RAG Ingest** | `backend/app/rag/{chunk,embed,ingest}.py`, `routers/policies.py`, `frontend/src/pages/Admin.tsx` | 1 |
| A4 | **RAG Query** | `backend/app/rag/retrieve.py`, `routers/chat.py`, `frontend/src/pages/Chat.tsx` | 1 |
| A5 | **Evaluator** | `backend/app/agents/evaluator.py`, `redact.py`, `parse.py`, `routers/{resumes,evaluate}.py` | 1 |
| A6 | **Jobs/JD** | `backend/app/agents/jd.py`, `routers/jobs.py`, `frontend/src/pages/{Jobs,NewJob}.tsx` | 1 |
| A7 | **Seed + Smoke** | `backend/scripts/{seed,smoke}.py`, `seed/**` | 1 |
| A8 | **Board + Kit** | `frontend/src/pages/{JobBoard,EvalDetail}.tsx`, `backend/app/agents/interview_kit.py`, `routers/interview_kit.py` | 2 |
| A9 | **Analytics** | `backend/app/routers/analytics.py`, `frontend/src/pages/Analytics.tsx` | 2 |
| — | **Integrator** (main thread) | Nothing exclusively — gates, smoke runs, targeted fixes | 3 |

### Rules that make it work

1. **Disjoint file ownership.** If two agents would touch one file, it belongs to A0/A1/A2 and is written in Wave 0. That is the entire conflict-avoidance strategy.
2. **Contract-first.** Wave 1 cannot start until Wave 0 reports done. It is the only real serialization point.
3. **Mock at the seam.** Wave-1 agents code against types and the seeded DB, never against another agent's running code. A4 does not wait for A3 — it queries chunks A7's seed script inserted.
4. **Own your slice end to end.** Each feature agent writes its router *and* its page. No backend-agent/frontend-agent split — that split manufactures a handoff, and handoffs are what we're eliminating.
5. **Fixed report format.** Files written · routes exposed · one command to exercise it · anything stubbed. Nothing else.
6. **Only A1 edits dependency files.** A Wave-1 agent needing a package reports it; A1 adds it. Concurrent `requirements.txt` edits are the classic parallel-build corruption.
7. **Integrator never writes features.** It runs the smoke test, reads failures, dispatches targeted fix agents. Writing code from the main thread while 5 agents run is how conflicts appear.
8. **Every agent runs inside the containers** — `docker compose exec backend ...`. "Works on my host" is not a passing state.

### Wave schedule

```
WAVE 0  (T+0 → T+10)   3 AGENTS PARALLEL
  A1 Infra      compose + Dockerfiles + deps ─┐
  A0 Contract   schemas + db + provider ──────┼──► CONTRACT FREEZE
  A2 Scaffold   app shell + routing ──────────┘    gate: `docker compose up` healthy,
                                                   /api/health green, shell renders

WAVE 1  (T+10 → T+40)  5 AGENTS FULLY PARALLEL
  A3 RAG Ingest ─┐
  A4 RAG Query ──┤
  A5 Evaluator ──┼──► gate: every Wave-1 route returns a contract-shaped response
  A6 Jobs/JD ────┤
  A7 Seed+Smoke ─┘     (A7 lands first — its fixtures unblock everyone's manual testing)

WAVE 2  (T+40 → T+52)  2 AGENTS PARALLEL   (depend on Wave-1 data shapes, not Wave-1 code)
  A8 Board + Interview Kit ─┐
  A9 Analytics ─────────────┴──► gate: all 6 features render real data

WAVE 3  (T+52 → T+60)  INTEGRATOR
  smoke → ≤2 parallel targeted fix agents → demo rehearsal
```

### Dispatch prompt template (use verbatim, one per agent)

> You are **{AGENT_NAME}**, one of 10 agents building an HR AI app in parallel.
> **Read first:** `PRD.md` §4 (your feature), §8 (schema), §9 (contract), §10 (AI spec).
> **You own exactly these files:** `{FILE_LIST}`. **Do not create, edit, or delete any file outside that list** — another agent owns it and your write will be discarded.
> **Do not edit `requirements.txt`, `package.json`, `docker-compose.yml`, or any Dockerfile.** Need a package? Say so in your report; A1 adds it.
> Import types from `backend/app/schemas.py` / `frontend/src/types.ts`. Do not modify them. Do not modify `db/init.sql`.
> All LLM calls go through `app.llm.provider.get_agent(...)`. Never construct an OpenAI client directly. Never hardcode a model name — read it from settings.
> Run and test inside the container: `docker compose exec backend pytest ...`, `docker compose exec frontend npm run build`.
> **Done means:** your §4 acceptance criterion passes, your route returns a contract-shaped response, and `ruff check` / `tsc --noEmit` are clean for your files.
> **Report:** files written · routes exposed · one command to exercise it · anything stubbed. Nothing else.

---

## 12. Containerization

Four services. `docker compose up --build` is the only command a reviewer needs.

| Service | Image / build | Port | Notes |
|---|---|---|---|
| `db` | `pgvector/pgvector:pg17` | 5432 | `db/init.sql` auto-applied; named volume `pgdata`; healthcheck `pg_isready` |
| `backend` | `python:3.12-slim`, multi-stage | 8000 | uvicorn `--reload` in dev; source bind-mounted; `depends_on: db healthy`; embedding model baked in at build |
| `frontend` | dev: `node:22-alpine` + Vite · prod: build → `nginx:alpine` | 5173 / 80 | `/api` proxied to backend so the browser never needs CORS |
| `ollama` | `ollama/ollama` | 11434 | **`profiles: [offline]`** — only starts with `--profile offline`. Keeps the default `up` fast |

Design notes that matter:

- **Layer caching:** dependency files copy and install *before* application source, so a code change doesn't reinstall the world. This is what keeps the rebuild loop under 10s during the hour.
- **The embedding model is downloaded at image build, not first request.** Otherwise the first user query pays a 130MB download and the demo stalls on stage.
- **Healthchecks gate startup order.** `backend` waits for `db` healthy; `frontend` waits for `backend` healthy. Without this, the backend crashes on a cold `docker compose up` and everyone blames the code.
- **Non-root user** in both app images.
- **No secrets in the image.** Keys arrive via `.env`, which is gitignored; `.env.example` is committed.
- **One volume, `pgdata`.** `docker compose down -v` is the documented reset.

Files delivered alongside this PRD: `docker-compose.yml`, `docker-compose.override.yml` (dev), `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, `.env.example`, `Makefile`.

---

## 13. Minute-by-Minute

| Time | What | Who |
|---|---|---|
| 00–10 | Compose + Dockerfiles + deps; schemas + init.sql + provider; app shell | A1, A0, A2 |
| **10** | **GATE: contract freeze.** `docker compose up` healthy, `/api/health` green, shell renders | Integrator |
| 10–16 | Seed fixtures: 3 policy docs, 2 JDs, 5 resumes, embeddings precomputed | A7 |
| 10–36 | Wave 1 — 5 agents in parallel | A3–A7 |
| **36** | **GATE:** every Wave-1 route returns a contract-shaped response | Integrator |
| 36–40 | Smoke run; dispatch fixes for Wave-1 gaps | Integrator |
| 40–52 | Wave 2 — board, eval detail, interview kit, analytics | A8, A9 |
| **52** | **GATE:** all 6 features render real data | Integrator |
| 52–57 | Full smoke pass; ≤2 parallel fix agents on failures | Integrator |
| 57–60 | Demo rehearsal against §14 | Integrator |

Slack is deliberate: 26 minutes for Wave-1 work that should take 20. One agent will hit something, and a wave gate moves at the speed of its slowest member.

---

## 14. Demo Script & Definition of Done

**Demo (4 minutes):**

1. `docker compose up` — four containers healthy, `/api/health` shows `provider: openrouter`, `tool_calling: ok`, and today's request budget.
2. `/admin` — upload a policy PDF, chunk count appears.
3. `/chat` — *"How many casual leave days can carry forward into the next year, and by when must they be used?"* → cited answer → click the chip → drawer shows the exact clause. **Use this wording, not a casual paraphrase** — see the retrieval limitation in §15.
4. `/chat` — *"What's the WFH policy for contractors?"* (not in corpus) → clean refusal, no fabrication. **This is the slide that sells it.**

   Both lines above are measured against the seeded corpus and behave as described. Ad-libbing a reworded question on stage is the one way to make this section misbehave.
5. `/jobs/new` — generate a Senior Backend Engineer JD; inclusive-language pass flags a phrase.
6. `/jobs/{id}` — drop 5 resumes → rows move `queued → scored` live, two at a time under the rate limiter. (The board already holds the pre-evaluated seed cohort, so it is never empty if the budget runs dry.)
7. Top candidate → rubric table with verbatim evidence quotes, gaps, recommendation, and the *"scored on redacted text"* badge.
8. "Generate interview kit" → 8 questions targeting that candidate's specific gaps.
9. `/analytics` — score distribution, skill-gap chart, token usage by provider.
10. Closing line: **total inference spend, $0.**

**Definition of done:**

- [ ] `docker compose up --build` from a clean clone → 4 healthy containers, no manual steps
- [ ] `make seed` populates 3 policies, 2 jobs, 5 resumes
- [ ] `make smoke` passes: 10 grounded answers, 2 refusals, 5 evaluations, all evidence quotes substring-verified
- [ ] All 6 features reachable from the nav shell, none stubbed on the happy path
- [ ] `ruff check` and `tsc --noEmit` clean
- [ ] Every evaluation wrote an `audit_log` row with provider, model, rubric version, tool trace
- [ ] `docker compose --profile offline up` runs the whole app against Ollama with no API key set

---

## 15. Risks & Cut Lines

| Risk | Likelihood | Mitigation |
|---|---|---|
| **OpenRouter 50-req/day cap exhausted mid-build** | **High** | Server-side daily counter with a visible used/remaining figure; seed evaluations committed, not generated; rerank off by default; scoring folded into one typed output. Escape hatch: one-time $10 credit → 1,000/day, or flip `LLM_PROVIDER=groq` (1,000/day free) |
| Chosen free model doesn't support tool calling | **High** | Startup probe fails loudly at boot with the list of known-good free models. Never discovered at T+45 |
| Free model returns 502/503 under load | Medium | 5xx treated identically to 429 in the retry path; failover to Groq |
| Rate limit hit mid-demo | Medium | Token bucket at 18 RPM; concurrency pool of 2; automatic failover to `LLM_FALLBACK_PROVIDER` |
| 70B free model ignores the citation format | Medium | Pydantic AI typed output + automatic retry; two worked examples in the system prompt; `grounded: false` rather than a bad answer |
| Free model is weak at multi-tool loops | Medium | Two flat, read-only tools; scoring is not a tool; 6-call cap; partial results are a valid outcome, not a crash |
| First `docker compose up` is slow (image pulls + model download) | **High** | **Pull and build at T-10, before the clock starts.** Document it. Nothing else in the plan is this easy to de-risk |
| Free tiers train on submitted data | Certain | Synthetic seed data only; Ollama profile documented as the private path; warning in the `/admin` UI |
| Agent writes outside its lane | Medium | Ownership table in the dispatch prompt; Integrator reviews `git status` at every gate and reverts strays |
| PDF parsing garbage on a real resume | Medium | Seed fixtures are markdown; PDF is the demo path only, with 2 pre-tested files |
| Provider changes its free tier next week | Medium | Five providers behind one interface; switching is one env var |

### Known limitation — retrieval is phrasing-sensitive

The grounded/refusal decision rests on a cosine-distance floor (`VEC_MAX_DISTANCE = 0.30`). Measured across 34 questions, **the must-answer and must-refuse sets overlap**: rewording the same question moves its distance by up to ~0.20, which is larger than the gap between a real in-corpus question and a real out-of-corpus one.

Concretely, after clause-level chunking and query-alignment enrichment narrowed the overlap by ~60%:

| Question | Should | Distance |
|---|---|---|
| "How many casual leave days can carry forward into the next year…" | answer | 0.0918 |
| Worst seeded in-corpus question | answer | 0.187 |
| "How many casual leaves carry forward?" (casual paraphrase) | answer | 0.3144 |
| "Do I need a doctor's note if I'm out sick?" | answer | 0.3157 |
| "Does the company reimburse relocation expenses…" (short form) | **refuse** | 0.3016 |
| "…when an employee moves cities for a new role?" (seeded form) | **refuse** | 0.3202 |

A single absolute threshold cannot satisfy all six rows. Relative-gap, z-score, and FTS-agreement mechanisms were each measured and each showed reversals; the z-score separation *degraded* as probes were added, indicating it was fitting noise rather than signal.

**The floor stays at 0.30 deliberately.** It keeps every seeded refusal correct with real margin, at the cost of refusing some casual paraphrases the corpus does answer. That asymmetry is the right one for an HR policy assistant: a refusal is a graceful failure that routes the employee to a human, while a confidently wrong answer carrying real citations is the most damaging output this feature can produce. Raising the floor to 0.32 would fix the paraphrases and leave a 0.0002 margin on a mandatory refusal — a coin flip, not a threshold.

**To actually close this** (out of scope for the one-hour build): embed a synthetic question per clause rather than the clause prose, or move to a stronger retrieval model with a reranker. Both attack the short-query/long-passage mismatch at its source instead of tuning a constant.

**Cut order when time runs out** — drop from the bottom, never the top:

1. F6 NL→SQL query box *(keep the 4 static charts)*
2. F6 entirely
3. F5 side-by-side compare *(keep the board)*
4. SSE live status *(fall back to polling every 2s)*
5. F4 interview kit
6. F3 JD Studio *(fall back to seeded JDs)*

**Never cut:** F1 grounded answers with citations, F1 refusal path, F2 evaluation with verified evidence quotes, F2 redaction + audit log. Those four *are* the product.

---

## Open Questions

1. **OpenRouter credit** — have you ever purchased $10 of OpenRouter credit on this account? If yes, the free-model cap is 1,000 req/day and §10's budget discipline becomes belt-and-braces. If no, it's 50/day and every mechanism in §10 is load-bearing. This is the single answer that most changes how tight the hour feels.
2. **Policy corpus** — real HR documents, or A7 generates 3 realistic synthetic ones?
3. **Demo machine RAM** — the Ollama profile with a 7B model wants ~8GB free. If the demo box is tight, Groq stays mandatory and the offline profile is documentation only.

---

## Sources (free-tier figures, verified 2026-09-18)

- [Free LLM API in 2026: 13 Options Ranked and Compared — OpenRouter](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
- [Free LLM API: real limits and catches, compared (2026)](https://continuumcode.ai/guides/free-llm-api/)
- [Best Free LLM API Tiers in 2026: Groq, Cerebras, GitHub Models & More](https://wetheflywheel.com/en/ai-model-access/free-llm-api-tiers-2026/)
- [Free LLM API 2026: 17 Tested, 7 Need No Card](https://klymentiev.com/blog/free-llm-api)
- [Best Free Embedding Model APIs in 2026](https://www.ainomam.com/post/embedding-model-api-free-2026)
- [Jina AI Embeddings](https://jina.ai/embeddings/)
