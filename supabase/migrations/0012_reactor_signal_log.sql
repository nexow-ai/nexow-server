-- Persistent log of every reactor signal evaluation (trades + skips)
CREATE TABLE reactor_signal_log (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  reactor_config_id uuid NOT NULL REFERENCES reactor_configs(id) ON DELETE CASCADE,
  instrument text NOT NULL,
  timeframe text NOT NULL DEFAULT 'H1',
  signal_type text NOT NULL,  -- 'buy', 'sell', 'hold'
  confidence numeric DEFAULT 0,
  reason text,
  candle_ts timestamptz,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX idx_signal_log_reactor ON reactor_signal_log(reactor_config_id, created_at DESC);
CREATE INDEX idx_signal_log_created ON reactor_signal_log(created_at DESC);
