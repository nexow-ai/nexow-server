-- Allow trades to be linked to reactor configs (not just agents).
-- agent_id becomes nullable; one of agent_id / reactor_config_id must be set.

ALTER TABLE trades
    ALTER COLUMN agent_id DROP NOT NULL;

ALTER TABLE trades
    ADD COLUMN reactor_config_id UUID REFERENCES reactor_configs(id) ON DELETE CASCADE;

CREATE INDEX idx_trades_reactor ON trades(reactor_config_id);

ALTER TABLE trades
    ADD CONSTRAINT chk_trade_owner
    CHECK (agent_id IS NOT NULL OR reactor_config_id IS NOT NULL);
