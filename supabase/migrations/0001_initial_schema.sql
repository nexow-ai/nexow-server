-- ============================================================================
-- Nexow — Consolidated Initial Schema
-- ============================================================================
-- This is the full schema as of the multi-repo migration.
-- Consolidated from the legacy monorepo's 7 incremental migrations into
-- a single clean starting point.
-- ============================================================================


-- ============================================================================
-- Enums
-- ============================================================================

CREATE TYPE agent_type    AS ENUM ('bot', 'agent');
CREATE TYPE agent_status  AS ENUM ('active', 'paused', 'killed');
CREATE TYPE trade_direction AS ENUM ('buy', 'sell');
CREATE TYPE trade_status  AS ENUM ('open', 'closed');
CREATE TYPE subscription_status AS ENUM ('active', 'paused', 'canceled', 'past_due', 'trialing', 'incomplete');
CREATE TYPE subscription_tier   AS ENUM ('free', 'starter', 'pro', 'elite');


-- ============================================================================
-- Profiles (extends auth.users)
-- ============================================================================

CREATE TABLE profiles (
    id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    username     TEXT UNIQUE NOT NULL,
    display_name TEXT,
    avatar_url   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_profiles_username ON profiles(username);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
    _username TEXT;
BEGIN
    _username := COALESCE(
        NEW.raw_user_meta_data->>'username',
        split_part(NEW.email, '@', 1)
    );

    IF EXISTS (SELECT 1 FROM public.profiles WHERE username = _username) THEN
        _username := _username || '_' || substr(gen_random_uuid()::text, 1, 8);
    END IF;

    INSERT INTO public.profiles (id, username, display_name)
    VALUES (
        NEW.id,
        _username,
        COALESCE(NEW.raw_user_meta_data->>'display_name', _username)
    );

    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    RAISE LOG 'handle_new_user failed for user %: %', NEW.id, SQLERRM;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION handle_new_user();


-- ============================================================================
-- Agents (trading bot / AI agent definitions)
-- ============================================================================

CREATE TABLE agents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id          UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT,
    type                agent_type NOT NULL DEFAULT 'bot',
    config              JSONB NOT NULL DEFAULT '{}',
    prompt              TEXT,

    -- Primary instrument + timeframe (convenience columns)
    instrument          TEXT NOT NULL DEFAULT 'EUR_USD',
    timeframe           TEXT NOT NULL DEFAULT 'M5',

    -- Portfolio: array of {instrument, timeframe} objects
    instruments         JSONB DEFAULT '[{"instrument": "EUR_USD", "timeframe": "M5"}]',

    -- LLM config (for AI agents)
    llm_provider        TEXT DEFAULT 'openai',
    llm_model           TEXT DEFAULT 'gpt-4o-mini',
    evaluation_schedule TEXT DEFAULT 'every_tick',

    status              agent_status NOT NULL DEFAULT 'paused',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agents_creator     ON agents(creator_id);
CREATE INDEX idx_agents_status      ON agents(status);
CREATE INDEX idx_agents_instrument  ON agents(instrument);
CREATE INDEX idx_agents_instruments ON agents USING gin(instruments);


-- ============================================================================
-- Trades (signal log — return-% accounting)
-- ============================================================================

CREATE TABLE trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    instrument      TEXT NOT NULL,
    direction       trade_direction NOT NULL,
    entry_price     NUMERIC(18,8) NOT NULL,
    exit_price      NUMERIC(18,8),
    return_pct      NUMERIC(10,4),
    stop_loss_pct   NUMERIC(5,2),
    take_profit_pct NUMERIC(5,2),
    status          trade_status NOT NULL DEFAULT 'open',
    backtest_id     UUID,  -- FK added after backtests table
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ
);

CREATE INDEX idx_trades_agent       ON trades(agent_id);
CREATE INDEX idx_trades_status      ON trades(status);
CREATE INDEX idx_trades_opened      ON trades(opened_at DESC);
CREATE INDEX idx_trades_backtest_id ON trades(backtest_id);


-- ============================================================================
-- Agent Performance (leaderboard stats — return-% based)
-- ============================================================================

CREATE TABLE agent_performance (
    agent_id         UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    total_trades     INT NOT NULL DEFAULT 0,
    winning_trades   INT NOT NULL DEFAULT 0,
    win_rate         NUMERIC(5,2)  NOT NULL DEFAULT 0.00,
    total_return_pct NUMERIC(10,4) NOT NULL DEFAULT 0.0000,
    avg_return_pct   NUMERIC(10,4) NOT NULL DEFAULT 0.0000,
    max_drawdown     NUMERIC(5,2)  NOT NULL DEFAULT 0.00,
    sharpe_ratio     NUMERIC(8,4)  NOT NULL DEFAULT 0.00,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================================
-- Copy Subscriptions (copy-trading)
-- ============================================================================

CREATE TABLE copy_subscriptions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    copier_id      UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    agent_id       UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    allocation_pct NUMERIC(5,2) NOT NULL DEFAULT 10.00,
    status         subscription_status NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(copier_id, agent_id)
);

CREATE INDEX idx_copy_subs_copier ON copy_subscriptions(copier_id);
CREATE INDEX idx_copy_subs_agent  ON copy_subscriptions(agent_id);
CREATE INDEX idx_copy_subs_active ON copy_subscriptions(agent_id) WHERE status = 'active';


-- ============================================================================
-- Subscriptions (billing plans)
-- ============================================================================

CREATE TABLE subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    tier                    subscription_tier NOT NULL DEFAULT 'free',
    status                  subscription_status NOT NULL DEFAULT 'active',

    -- Stripe references
    stripe_customer_id      TEXT UNIQUE,
    stripe_subscription_id  TEXT UNIQUE,
    stripe_price_id         TEXT,

    -- Billing period
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT false,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT unique_user_subscription UNIQUE (user_id)
);

CREATE INDEX idx_subscriptions_user_id         ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_stripe_customer ON subscriptions(stripe_customer_id);
CREATE INDEX idx_subscriptions_stripe_sub      ON subscriptions(stripe_subscription_id);


-- ============================================================================
-- AI Credits
-- ============================================================================

CREATE TABLE ai_credits (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    credits_limit INTEGER NOT NULL DEFAULT 100,
    credits_used  INTEGER NOT NULL DEFAULT 0,
    period_start  TIMESTAMPTZ NOT NULL DEFAULT now(),
    period_end    TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT unique_user_credits UNIQUE (user_id)
);

CREATE INDEX idx_ai_credits_user_id ON ai_credits(user_id);


-- ============================================================================
-- Credit Usage Log
-- ============================================================================

CREATE TABLE credit_usage_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    agent_id     UUID REFERENCES agents(id) ON DELETE SET NULL,
    action       TEXT NOT NULL,
    credits_used INTEGER NOT NULL,
    description  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_credit_usage_user    ON credit_usage_log(user_id);
CREATE INDEX idx_credit_usage_agent   ON credit_usage_log(agent_id);
CREATE INDEX idx_credit_usage_created ON credit_usage_log(created_at);


-- ============================================================================
-- Agent Logs (real-time console output)
-- ============================================================================

CREATE TABLE agent_logs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id   UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    metadata   JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_logs_agent_id   ON agent_logs(agent_id);
CREATE INDEX idx_agent_logs_created_at ON agent_logs(created_at DESC);


-- ============================================================================
-- Backtests (historical simulations)
-- ============================================================================

CREATE TABLE backtests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id         UUID REFERENCES agents(id) ON DELETE CASCADE,
    creator_id       UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    config           JSONB NOT NULL DEFAULT '{}',
    instruments      JSONB NOT NULL DEFAULT '[]',
    exit_config      JSONB DEFAULT '{}',
    period_start     TIMESTAMPTZ NOT NULL,
    period_end       TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'completed', 'failed')),
    progress_pct     SMALLINT NOT NULL DEFAULT 0,
    total_trades     INT,
    total_return_pct NUMERIC(10,4),
    win_rate         NUMERIC(5,2),
    max_drawdown     NUMERIC(5,2),
    sharpe_ratio     NUMERIC(8,4),
    profit_factor    NUMERIC(8,4),
    equity_curve     JSONB DEFAULT '[]',
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_backtests_agent_id   ON backtests(agent_id);
CREATE INDEX idx_backtests_creator_id ON backtests(creator_id);
CREATE INDEX idx_backtests_status     ON backtests(status);

-- Now add the FK from trades -> backtests
ALTER TABLE trades
    ADD CONSTRAINT fk_trades_backtest FOREIGN KEY (backtest_id) REFERENCES backtests(id) ON DELETE CASCADE;


-- ============================================================================
-- Public view (hides config from non-owners)
-- ============================================================================

CREATE VIEW agents_public AS
SELECT
    id, creator_id, name, description, type, instrument, instruments, timeframe,
    status, llm_provider, llm_model, evaluation_schedule, created_at, updated_at
FROM agents;


-- ============================================================================
-- Triggers — auto-update updated_at
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_profiles_updated_at
    BEFORE UPDATE ON profiles FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_agents_updated_at
    BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_agent_performance_updated_at
    BEFORE UPDATE ON agent_performance FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ============================================================================
-- Trigger — auto-create subscription + credits for new users
-- ============================================================================

CREATE OR REPLACE FUNCTION create_user_subscription()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO subscriptions (user_id, tier, status)
    VALUES (NEW.id, 'free', 'active');

    INSERT INTO ai_credits (user_id, credits_limit, credits_used)
    VALUES (NEW.id, 100, 0);

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_profile_created_subscription
    AFTER INSERT ON profiles
    FOR EACH ROW EXECUTE FUNCTION create_user_subscription();


-- ============================================================================
-- Functions — credits
-- ============================================================================

CREATE OR REPLACE FUNCTION consume_credits(
    p_user_id UUID,
    p_amount INTEGER,
    p_action TEXT,
    p_agent_id UUID DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    v_remaining INTEGER;
BEGIN
    SELECT (credits_limit - credits_used) INTO v_remaining
    FROM ai_credits
    WHERE user_id = p_user_id;

    IF v_remaining IS NULL OR v_remaining < p_amount THEN
        RETURN FALSE;
    END IF;

    UPDATE ai_credits
    SET credits_used = credits_used + p_amount,
        updated_at = now()
    WHERE user_id = p_user_id;

    INSERT INTO credit_usage_log (user_id, agent_id, action, credits_used, description)
    VALUES (p_user_id, p_agent_id, p_action, p_amount, p_description);

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION reset_user_credits(
    p_user_id UUID,
    p_new_limit INTEGER,
    p_period_start TIMESTAMPTZ,
    p_period_end TIMESTAMPTZ
)
RETURNS VOID AS $$
BEGIN
    UPDATE ai_credits
    SET credits_limit = p_new_limit,
        credits_used = 0,
        period_start = p_period_start,
        period_end = p_period_end,
        updated_at = now()
    WHERE user_id = p_user_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Username availability check
CREATE OR REPLACE FUNCTION check_username_available(desired_username TEXT)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN NOT EXISTS (
        SELECT 1 FROM public.profiles WHERE username = lower(trim(desired_username))
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;


-- ============================================================================
-- Row Level Security
-- ============================================================================

-- Profiles
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Profiles are publicly readable"
    ON profiles FOR SELECT USING (true);
CREATE POLICY "Users can insert own profile"
    ON profiles FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "Users can update own profile"
    ON profiles FOR UPDATE USING (auth.uid() = id);

-- Agents
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Agents metadata is publicly readable"
    ON agents FOR SELECT USING (true);
CREATE POLICY "Only creator can insert agents"
    ON agents FOR INSERT WITH CHECK (auth.uid() = creator_id);
CREATE POLICY "Only creator can update agents"
    ON agents FOR UPDATE USING (auth.uid() = creator_id);
CREATE POLICY "Only creator can delete agents"
    ON agents FOR DELETE USING (auth.uid() = creator_id);

-- Trades
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Trade creator can read own trades"
    ON trades FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM agents
            WHERE agents.id = trades.agent_id
            AND agents.creator_id = auth.uid()
        )
    );
CREATE POLICY "Active copiers can read trades"
    ON trades FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM copy_subscriptions cs
            WHERE cs.agent_id = trades.agent_id
            AND cs.copier_id = auth.uid()
            AND cs.status = 'active'
        )
    );
CREATE POLICY "Service role can manage trades"
    ON trades FOR ALL USING (auth.role() = 'service_role');

-- Agent Performance
ALTER TABLE agent_performance ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Performance is publicly readable"
    ON agent_performance FOR SELECT USING (true);
CREATE POLICY "Service role can manage performance"
    ON agent_performance FOR ALL USING (auth.role() = 'service_role');

-- Copy Subscriptions
ALTER TABLE copy_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Copiers can read own subscriptions"
    ON copy_subscriptions FOR SELECT USING (auth.uid() = copier_id);
CREATE POLICY "Agent creators can see who copies them"
    ON copy_subscriptions FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM agents
            WHERE agents.id = copy_subscriptions.agent_id
            AND agents.creator_id = auth.uid()
        )
    );
CREATE POLICY "Copiers can insert subscriptions"
    ON copy_subscriptions FOR INSERT WITH CHECK (auth.uid() = copier_id);
CREATE POLICY "Copiers can update own subscriptions"
    ON copy_subscriptions FOR UPDATE USING (auth.uid() = copier_id);
CREATE POLICY "Copiers can delete own subscriptions"
    ON copy_subscriptions FOR DELETE USING (auth.uid() = copier_id);

-- Subscriptions
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own subscription"
    ON subscriptions FOR SELECT USING (auth.uid() = user_id);

-- AI Credits
ALTER TABLE ai_credits ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own credits"
    ON ai_credits FOR SELECT USING (auth.uid() = user_id);

-- Credit Usage Log
ALTER TABLE credit_usage_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own usage log"
    ON credit_usage_log FOR SELECT USING (auth.uid() = user_id);

-- Agent Logs
ALTER TABLE agent_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view logs for their agents"
    ON agent_logs FOR SELECT USING (
        agent_id IN (
            SELECT id FROM agents WHERE creator_id = auth.uid()
        )
    );

-- Backtests
ALTER TABLE backtests ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own backtests"
    ON backtests FOR SELECT USING (creator_id = auth.uid());
CREATE POLICY "Users can insert their own backtests"
    ON backtests FOR INSERT WITH CHECK (creator_id = auth.uid());
CREATE POLICY "Users can update their own backtests"
    ON backtests FOR UPDATE USING (creator_id = auth.uid());
CREATE POLICY "Users can delete their own backtests"
    ON backtests FOR DELETE USING (creator_id = auth.uid());
CREATE POLICY "Service role has full access to backtests"
    ON backtests FOR ALL USING (auth.role() = 'service_role');


-- ============================================================================
-- Realtime
-- ============================================================================

ALTER PUBLICATION supabase_realtime ADD TABLE trades;
ALTER PUBLICATION supabase_realtime ADD TABLE agent_performance;
ALTER PUBLICATION supabase_realtime ADD TABLE agent_logs;
