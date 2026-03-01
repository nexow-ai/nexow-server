-- ============================================================================
-- Forex Prices 1-minute (Massive.com flat files ingestion)
-- ============================================================================

CREATE TABLE forex_prices_1m (
    instrument  TEXT NOT NULL,                 -- e.g. "EUR_USD"
    ts          TIMESTAMPTZ NOT NULL,          -- minute candle open time
    open        NUMERIC(12,6) NOT NULL,
    high        NUMERIC(12,6) NOT NULL,
    low         NUMERIC(12,6) NOT NULL,
    close       NUMERIC(12,6) NOT NULL,
    volume      INT NOT NULL DEFAULT 0,
    transactions INT NOT NULL DEFAULT 0,

    PRIMARY KEY (instrument, ts)
);

CREATE INDEX idx_forex_1m_ts ON forex_prices_1m(ts DESC);
CREATE INDEX idx_forex_1m_instrument ON forex_prices_1m(instrument);

ALTER TABLE forex_prices_1m ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Forex prices are publicly readable"
    ON forex_prices_1m FOR SELECT USING (true);
CREATE POLICY "Service role can manage forex prices"
    ON forex_prices_1m FOR ALL USING (auth.role() = 'service_role');
