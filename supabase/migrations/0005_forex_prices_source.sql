-- Add source column to distinguish massive flat files vs oanda real-time fill
ALTER TABLE forex_prices_1m ADD COLUMN source TEXT NOT NULL DEFAULT 'oanda';

CREATE INDEX idx_forex_1m_source ON forex_prices_1m(source);
