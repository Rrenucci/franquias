-- ============================================================
-- Franchise Intelligence — Franchise Details v0.3
-- Guarda o FAQ estruturado (JSON-LD) de cada franquia — payback,
-- taxas, lucratividade etc., em texto original (não parseado à força
-- em número, pra não arriscar interpretar errado um dado que vai virar
-- explicação pro investidor). Os campos numéricos existentes
-- (royalty_pct, payback_months_min/max, units_count, avg_monthly_revenue)
-- continuam sendo preenchidos best-effort a partir desse texto.
-- ============================================================

ALTER TABLE franchises ADD COLUMN IF NOT EXISTS faq_data JSONB;
ALTER TABLE franchises ADD COLUMN IF NOT EXISTS faq_fetched_at TIMESTAMPTZ;
