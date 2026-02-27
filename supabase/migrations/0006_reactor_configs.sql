-- Reactor configurations — one per user per instrument
CREATE TABLE reactor_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    instrument TEXT NOT NULL DEFAULT 'EUR_USD',
    trades_per_day INT NOT NULL DEFAULT 3,
    risk_mode TEXT NOT NULL DEFAULT 'percentage' CHECK (risk_mode IN ('percentage', 'fixed')),
    risk_value NUMERIC(12,4) NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_reactor_user_instrument UNIQUE (user_id, instrument)
);

-- RLS
ALTER TABLE reactor_configs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own reactor configs"
    ON reactor_configs FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own reactor configs"
    ON reactor_configs FOR INSERT
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own reactor configs"
    ON reactor_configs FOR UPDATE
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own reactor configs"
    ON reactor_configs FOR DELETE
    USING (auth.uid() = user_id);

-- Auto-update updated_at
CREATE TRIGGER set_reactor_configs_updated_at
    BEFORE UPDATE ON reactor_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
