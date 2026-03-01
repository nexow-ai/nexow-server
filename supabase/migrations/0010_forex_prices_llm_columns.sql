-- Merge LLM analysis data into forex_prices_1m (eliminates snapshot_analyses table)

ALTER TABLE forex_prices_1m
    ADD COLUMN ai_technical    NUMERIC(4,2),
    ADD COLUMN ai_momentum     NUMERIC(4,2),
    ADD COLUMN ai_fundamental  NUMERIC(4,2),
    ADD COLUMN ai_structure    NUMERIC(4,2),
    ADD COLUMN ai_session      NUMERIC(4,2),
    ADD COLUMN ai_overall      NUMERIC(4,2),
    ADD COLUMN ai_direction    TEXT,
    ADD COLUMN ai_reasoning    TEXT,
    ADD COLUMN ai_model        TEXT,
    ADD COLUMN ai_tokens_in    INT,
    ADD COLUMN ai_tokens_out   INT,
    ADD COLUMN ai_duration_ms  INT,
    ADD COLUMN ai_analyzed_at  TIMESTAMPTZ;

-- Partial index: only scan rows that have been analyzed
CREATE INDEX idx_forex_1m_analyzed
    ON forex_prices_1m (instrument, ts DESC)
    WHERE ai_direction IS NOT NULL;

-- Migrate existing data from snapshot_analyses
UPDATE forex_prices_1m p
SET
    ai_technical   = sa.technical_score,
    ai_momentum    = sa.momentum_score,
    ai_fundamental = sa.fundamental_score,
    ai_structure   = sa.structure_score,
    ai_session     = sa.session_score,
    ai_overall     = sa.overall_score,
    ai_direction   = sa.direction,
    ai_reasoning   = sa.reasoning,
    ai_model       = sa.llm_model,
    ai_tokens_in   = sa.prompt_tokens,
    ai_tokens_out  = sa.completion_tokens,
    ai_duration_ms = sa.duration_ms,
    ai_analyzed_at = sa.created_at
FROM snapshot_analyses sa
WHERE p.instrument = sa.instrument AND p.ts = sa.timestamp;

-- Drop the old table
DROP TABLE snapshot_analyses;
