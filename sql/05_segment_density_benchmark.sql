-- Benchmark nacional de densidade de empresas por segmento — usado pra
-- calcular score_market_gap (a cidade tem MENOS empresas desse segmento do
-- que o típico pro tamanho dela, ou já está saturada?).
--
-- Vem 100% de dado já coletado (company_lifecycle, CNPJ da Receita Federal),
-- sem nova ingestão. Tabela pequena (uma linha por segmento), recalculada
-- por scripts/compute_segment_benchmarks.py.

CREATE TABLE IF NOT EXISTS segment_density_benchmark (
    segment                  TEXT PRIMARY KEY,
    avg_active_per_10k        NUMERIC,
    median_active_per_10k     NUMERIC,
    cities_sampled            INTEGER,
    updated_at                TIMESTAMPTZ DEFAULT NOW()
);
