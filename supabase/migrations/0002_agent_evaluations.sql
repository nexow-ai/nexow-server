-- ============================================================================
-- Agent Evaluations — per-run reasoning + token tracking (agents only)
-- ============================================================================

CREATE TABLE agent_evaluations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    instrument        TEXT NOT NULL,
    action            TEXT NOT NULL,
    confidence        NUMERIC(5,4) NOT NULL DEFAULT 0,
    reasoning         TEXT,
    technical_summary TEXT,
    sentiment_summary TEXT,
    data_sources_used JSONB DEFAULT '[]',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    llm_provider      TEXT,
    llm_model         TEXT,
    duration_ms       INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_evaluations_agent   ON agent_evaluations(agent_id);
CREATE INDEX idx_evaluations_created ON agent_evaluations(created_at DESC);


-- Link trades to the evaluation that triggered them (nullable, agents only)
ALTER TABLE trades ADD COLUMN evaluation_id UUID REFERENCES agent_evaluations(id) ON DELETE SET NULL;
CREATE INDEX idx_trades_evaluation ON trades(evaluation_id);


-- Cumulative token tracking on agent_performance
ALTER TABLE agent_performance ADD COLUMN total_tokens_used BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_performance ADD COLUMN total_evaluations  INT   NOT NULL DEFAULT 0;


-- ============================================================================
-- Row Level Security
-- ============================================================================

ALTER TABLE agent_evaluations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view evaluations for their agents"
    ON agent_evaluations FOR SELECT USING (
        agent_id IN (SELECT id FROM agents WHERE creator_id = auth.uid())
    );

CREATE POLICY "Service role can manage evaluations"
    ON agent_evaluations FOR ALL USING (auth.role() = 'service_role');


-- ============================================================================
-- Realtime
-- ============================================================================

ALTER PUBLICATION supabase_realtime ADD TABLE agent_evaluations;
