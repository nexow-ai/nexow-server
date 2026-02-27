-- Add reward_ratio to reactor_configs (risk/reward multiplier for TP calculation)
ALTER TABLE reactor_configs
    ADD COLUMN reward_ratio NUMERIC(4,2) NOT NULL DEFAULT 2.00;
