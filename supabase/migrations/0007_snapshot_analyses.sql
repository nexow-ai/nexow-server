-- LLM analyses of market snapshots (1 per instrument per minute)
CREATE TABLE snapshot_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    technical_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    momentum_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    fundamental_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    structure_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    session_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    overall_score NUMERIC(4,2) NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT 'hold',
    reasoning TEXT,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    llm_model TEXT,
    duration_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_snapshot_analysis UNIQUE (instrument, timestamp)
);

CREATE INDEX idx_snapshot_analyses_instrument_ts
    ON snapshot_analyses (instrument, timestamp DESC);

-- RLS — readable by all authenticated users
ALTER TABLE snapshot_analyses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Authenticated users can read analyses"
    ON snapshot_analyses FOR SELECT
    USING (auth.role() = 'authenticated');

-- Extend reactor_configs with personalization columns
ALTER TABLE reactor_configs
    ADD COLUMN timeframe TEXT NOT NULL DEFAULT 'H1',
    ADD COLUMN weight_technical NUMERIC(3,2) NOT NULL DEFAULT 0.30,
    ADD COLUMN weight_momentum NUMERIC(3,2) NOT NULL DEFAULT 0.20,
    ADD COLUMN weight_fundamental NUMERIC(3,2) NOT NULL DEFAULT 0.20,
    ADD COLUMN weight_structure NUMERIC(3,2) NOT NULL DEFAULT 0.20,
    ADD COLUMN weight_session NUMERIC(3,2) NOT NULL DEFAULT 0.10,
    ADD COLUMN confidence_threshold NUMERIC(3,2) NOT NULL DEFAULT 0.60;
