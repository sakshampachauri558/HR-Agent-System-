# PeopleOps Copilot

A containerized HR AI suite: grounded policy Q&A over your own documents, and
an agentic resume-vs-JD evaluator — built to run entirely on free LLM
inference.

## Quickstart

```bash
cp .env.example .env
make up
open http://localhost:5173
```

That's it. `docker compose up -d --build` builds the backend (FastAPI +
pgvector, FastEmbed model baked into the image) and the frontend (React +
Vite dev server), and starts Postgres. `make seed` loads sample policies,
jobs and pre-evaluated resumes; `make health` checks `/api/health`.

## Features

| # | Feature | What it does |
|---|---|---|
| F1 | Policy RAG Chat | Grounded Q&A over uploaded HR policy docs, with inline citations back to document + section. Refuses cleanly when the corpus doesn't cover the question. |
| F2 | Resume Evaluator Agent | An agentic, tool-using evaluator that scores a resume against a JD on a fixed rubric, with evidence-linked reasoning and a verified verdict. Scores on a PII-redacted view of the resume; every run writes an audit log. |
| F3 | JD Studio | Generates a full job description from a short form, including an inclusive-language pass that flags gendered/exclusionary phrasing. |
| F4 | Interview Kit Generator | Turns an evaluation's identified gaps into a candidate-specific set of interview questions. |
| F5 | Candidate Ranking Board | Per-job table of evaluated candidates with live SSE status as a batch of resumes moves through the pipeline. |
| F6 | HR Analytics | Dashboards over local evaluation data, plus a natural-language query box restricted to an allowlisted, read-only query path. |

## Free LLM providers

All hosted options are OpenAI-compatible and sit behind one client — switching
is a single env var (`LLM_PROVIDER`).

| Provider | Free tier | Card needed | Default model |
|---|---|---|---|
| **OpenRouter** *(default)* | 20 RPM · **50 free-model req/day**, rising to 1,000/day after a one-time $10 credit purchase | No | `meta-llama/llama-3.3-70b-instruct:free` |
| **Groq** *(fallback)* | 30 RPM · 1,000 req/day | No | `llama-3.3-70b-versatile` |
| **Google AI Studio** | Large context, lower request volume | No | `gemini-2.0-flash` |
| **Cerebras** | ~1M tokens/day, very fast — card-required since July 2026 | Yes | `llama-3.3-70b` |
| **Ollama** *(offline)* | Unlimited, local, no key, no network | No | `qwen2.5:7b-instruct` |

Embeddings are **FastEmbed** (`BAAI/bge-small-en-v1.5`), local and free —
baked into the backend image at build time, no key, no rate limit.

## Data handling

This application sends policy questions and resume text to a third-party model
provider, which may retain them under its own data policies. That is an
acceptable trade-off for the synthetic data this repository ships with; it is
**not** acceptable for real resumes or real employee questions.

**The only configuration where no data leaves the machine is the Ollama offline
profile** (`make offline`). Point the app at real HR data only under that
profile. The `/admin` page states the same thing at the point of upload, where
the decision is actually made.

Two design choices follow from this rather than being bolted on afterwards:
resume text is PII-redacted before the evaluator ever sees it (the model scores
a redacted document; the reviewer sees the original), and embeddings run locally
via FastEmbed, so document ingestion makes no network call at all.

## Operational limits

OpenRouter's free tier allows **50 model requests per day** before a one-time
$10 credit purchase raises it to 1,000. The app enforces its own daily counter
and returns a typed `llm_budget_exhausted` error rather than failing opaquely at
the provider. Mock and Ollama calls are excluded from that counter, since
neither consumes hosted quota.

Free model slugs move behind payment without notice — `llama-3.3-70b-instruct:free`
did during development. Set `LLM_MODEL` to swap models without a code change, and
verify a candidate first, since not every free model supports the tool calling the
evaluator agent requires:

```bash
docker compose exec -T -e LLM_PROVIDER=openrouter -e LLM_MODEL=<slug> \
  backend python -m scripts.smoke --probe
```
