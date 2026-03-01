-- ============================================================================
-- Economic Events (Forex Factory calendar scraping)
-- ============================================================================

CREATE TYPE event_impact AS ENUM ('high', 'medium', 'low', 'holiday', 'none');

CREATE TABLE economic_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date        DATE NOT NULL,
    time        TEXT,                          -- e.g. "08:30" or "All Day" or "Tentative"
    currency    TEXT NOT NULL,                 -- e.g. "USD", "EUR", "GBP"
    impact      event_impact NOT NULL DEFAULT 'none',
    event       TEXT NOT NULL,                 -- e.g. "Non-Farm Employment Change"
    actual      TEXT,                          -- e.g. "256K", "3.5%", or NULL if not yet released
    forecast    TEXT,                          -- e.g. "164K"
    previous    TEXT,                          -- e.g. "212K"
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Deduplicate: same event on same date for same currency
    CONSTRAINT uq_economic_event UNIQUE (date, currency, event)
);

CREATE INDEX idx_eco_events_date     ON economic_events(date DESC);
CREATE INDEX idx_eco_events_currency ON economic_events(currency);
CREATE INDEX idx_eco_events_impact   ON economic_events(impact);

-- Service role full access (server writes from scraper)
ALTER TABLE economic_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Economic events are publicly readable"
    ON economic_events FOR SELECT USING (true);
CREATE POLICY "Service role can manage economic events"
    ON economic_events FOR ALL USING (auth.role() = 'service_role');
