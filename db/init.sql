-- PeopleOps Copilot — schema
-- Owned by A0. Frozen after Wave 0. Applied automatically by the postgres
-- image on first boot of an empty `pgdata` volume
-- (docker-entrypoint-initdb.d only runs against a fresh volume — `make reset`
-- / `docker compose down -v` is the documented way to re-apply it).
--
-- Written to be safe to re-run by hand against a non-empty database too
-- (IF NOT EXISTS everywhere, a guarded CREATE ROLE) in case an agent runs
-- it manually mid-build.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------
-- documents — one row per uploaded policy source file
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title        TEXT NOT NULL,
  kind         TEXT NOT NULL,                      -- pdf|md|txt
  source_name  TEXT,
  char_count   INT,
  uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- chunks — section-aware, embedded, full-text-indexed policy chunks
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  UUID REFERENCES documents(id) ON DELETE CASCADE,
  section      TEXT,
  ordinal      INT,
  text         TEXT NOT NULL,
  start_char   INT,
  end_char     INT,
  token_count  INT,
  embedding    vector(384),                        -- bge-small-en-v1.5 (FastEmbed)
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin_idx ON chunks USING gin (tsv);
-- Supports fuzzy fallback / trigram search on raw chunk text (pg_trgm).
CREATE INDEX IF NOT EXISTS chunks_text_trgm_idx ON chunks USING gin (text gin_trgm_ops);

-- ---------------------------------------------------------------------
-- jobs — job descriptions (F3 output, F2 input)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title           TEXT NOT NULL,
  level           TEXT,
  location        TEXT,
  department      TEXT,
  description_md  TEXT,
  must_haves      JSONB NOT NULL DEFAULT '[]',
  nice_to_haves   JSONB NOT NULL DEFAULT '[]',
  min_years       INT,
  comp_min        INT,
  comp_max        INT,
  rubric_weights  JSONB,                            -- null => DEFAULT_RUBRIC (schemas.py)
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- resumes — uploaded candidates for a screening run
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resumes (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_label  TEXT NOT NULL,                   -- "Candidate A" — generated pseudonym
  file_name        TEXT,
  raw_text         TEXT,
  redacted_text    TEXT,
  sections         JSONB NOT NULL DEFAULT '{}',
  status           TEXT NOT NULL DEFAULT 'queued'    -- queued|parsing|evaluating|scored|failed
                     CHECK (status IN ('queued','parsing','evaluating','scored','failed')),
  error            TEXT,
  uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS resumes_status_idx ON resumes (status);

-- ---------------------------------------------------------------------
-- evaluations — F2 verdicts, one per (resume, job) pair
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluations (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  resume_id        UUID REFERENCES resumes(id) ON DELETE CASCADE,
  job_id           UUID REFERENCES jobs(id) ON DELETE CASCADE,
  fit_score        INT CHECK (fit_score BETWEEN 0 AND 100),
  recommendation   TEXT CHECK (recommendation IN ('strong_yes','yes','maybe','no')),
  criteria         JSONB,                            -- list[CriterionScore]
  matched_skills   JSONB NOT NULL DEFAULT '[]',
  gaps             JSONB NOT NULL DEFAULT '[]',
  summary          TEXT,
  provider         TEXT,
  model            TEXT,
  rubric_version   TEXT,
  prompt_version   TEXT,
  input_tokens     INT,
  output_tokens    INT,
  latency_ms       INT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (resume_id, job_id)
);

CREATE INDEX IF NOT EXISTS evaluations_job_id_idx ON evaluations (job_id);
CREATE INDEX IF NOT EXISTS evaluations_resume_id_idx ON evaluations (resume_id);

-- ---------------------------------------------------------------------
-- interview_kits — F4 output, one per evaluation
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interview_kits (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  evaluation_id  UUID REFERENCES evaluations(id) ON DELETE CASCADE,
  questions      JSONB,                              -- list[InterviewQuestion]
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS interview_kits_evaluation_id_idx ON interview_kits (evaluation_id);

-- ---------------------------------------------------------------------
-- audit_log — every LLM call and every evaluation decision, immutable
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type  TEXT,                                 -- e.g. "evaluation" | "llm_call" | "document"
  entity_id    UUID,
  action       TEXT,                                 -- e.g. "llm_call" | "evaluate" | "ingest"
  actor        TEXT,                                 -- feature/agent name, or "X-Demo-User" value
  tool_trace   JSONB,
  payload      JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The daily LLM budget counter and F6's usage chart both scan this by day.
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS audit_log_action_idx ON audit_log (action);

-- ---------------------------------------------------------------------
-- analytics_ro — read-only role for F6's NL→SQL path. No free-form SQL
-- from the LLM ever executes as anything but this role.
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
    CREATE ROLE analytics_ro NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro;

-- `resumes.raw_text` / `resumes.redacted_text` hold candidate PII (PRD §8).
-- The table-level GRANT above would otherwise let analytics_ro read them
-- too; the application-layer allowlist in routers/analytics.py already
-- keeps the NL->SQL path off those columns, but the role itself should
-- not be able to read them either -- a DB-level backstop in case that
-- allowlist is ever wrong. Revoke the blanket table grant on `resumes`
-- specifically and re-grant only the non-PII columns F6 actually needs.
REVOKE SELECT ON resumes FROM analytics_ro;
GRANT SELECT (id, candidate_label, file_name, status, uploaded_at) ON resumes TO analytics_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analytics_ro;
