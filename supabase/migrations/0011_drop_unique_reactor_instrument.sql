-- Allow multiple reactor configs per user per instrument
-- (e.g. different timeframes or weight strategies on the same pair)
ALTER TABLE reactor_configs DROP CONSTRAINT uq_reactor_user_instrument;
