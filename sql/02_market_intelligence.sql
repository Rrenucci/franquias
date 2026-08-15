-- ============================================================
-- Franchise Intelligence — Market Intelligence v0.2
-- Adiciona: poder de compra, custo de vida, infraestrutura,
-- e ciclo de vida de empresas (mortalidade/sobrevida por CNAE)
-- Execute depois do 01_schema.sql
-- ============================================================

-- ─── CIDADES: poder de compra, custo de vida, infraestrutura ─────────────────

ALTER TABLE cities ADD COLUMN IF NOT EXISTS cost_of_living_index   NUMERIC(6,2);  -- índice relativo, 100 = média nacional
ALTER TABLE cities ADD COLUMN IF NOT EXISTS avg_ticket_estimate    NUMERIC(10,2); -- ticket médio estimado (renda ajustada por custo de vida)
ALTER TABLE cities ADD COLUMN IF NOT EXISTS purchasing_power_index NUMERIC(6,2);  -- renda real / custo de vida local

ALTER TABLE cities ADD COLUMN IF NOT EXISTS sanitation_pct      NUMERIC(5,2);  -- % domicílios c/ saneamento adequado (IBGE MUNIC)
ALTER TABLE cities ADD COLUMN IF NOT EXISTS internet_access_pct NUMERIC(5,2);  -- % domicílios c/ internet
ALTER TABLE cities ADD COLUMN IF NOT EXISTS has_hospital        BOOLEAN;
ALTER TABLE cities ADD COLUMN IF NOT EXISTS has_shopping_mall   BOOLEAN;
ALTER TABLE cities ADD COLUMN IF NOT EXISTS infrastructure_score NUMERIC(5,2); -- score composto 0-100

-- ─── CICLO DE VIDA DE EMPRESAS (Receita Federal — Dados Abertos CNPJ) ────────
-- Mortalidade/sobrevida por segmento e cidade. É o dado que faltava pra
-- responder "outras franquias desse tipo já tentaram aqui e fecharam?"

CREATE TABLE IF NOT EXISTS company_lifecycle (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id             UUID REFERENCES cities(id) ON DELETE CASCADE,
    cnae_code           VARCHAR(7) NOT NULL,
    segment             TEXT,                    -- mapeado do CNAE pro segmento de franquia (mesma taxonomia de franchises.segment)

    opened_count        INTEGER DEFAULT 0,       -- total de CNPJs abertos no período observado
    closed_count        INTEGER DEFAULT 0,       -- total de CNPJs baixados
    active_count         INTEGER DEFAULT 0,       -- CNPJs ativos hoje

    avg_survival_months NUMERIC(6,1),             -- sobrevida média até baixa (empresas fechadas)
    mortality_rate_1y   NUMERIC(5,2),             -- % que fecharam em até 12 meses
    mortality_rate_3y   NUMERIC(5,2),             -- % que fecharam em até 36 meses

    is_franchise_flag   BOOLEAN DEFAULT false,    -- heurística: nome/razão social bate com franquia conhecida
    data_source         TEXT DEFAULT 'receita_federal',
    data_reference_date DATE,                     -- data-base do dump da Receita usado
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(city_id, cnae_code)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_city    ON company_lifecycle(city_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_cnae    ON company_lifecycle(cnae_code);
CREATE INDEX IF NOT EXISTS idx_lifecycle_segment ON company_lifecycle(segment);

-- ─── GAP DE OFERTA: densidade de empresas por segmento vs. esperado ──────────
-- View, não tabela — calcula em tempo real comparando a densidade local
-- (company_segments.total_companies / população) com a média do estado,
-- pra apontar segmentos "carentes" numa cidade.

CREATE OR REPLACE VIEW city_segment_gap AS
SELECT
    cs.city_id,
    cs.segment,
    c.state_code,
    c.population,
    cs.total_companies,
    cs.franchise_companies,
    ROUND(cs.total_companies::NUMERIC / NULLIF(c.population, 0) * 10000, 3)              AS companies_per_10k,
    ROUND(AVG(cs.total_companies::NUMERIC / NULLIF(c.population, 0) * 10000)
          OVER (PARTITION BY cs.segment, c.state_code), 3)                                AS state_avg_companies_per_10k
FROM city_segments cs
JOIN cities c ON c.id = cs.city_id;

-- ─── RECOMENDAÇÕES: novos eixos de score ─────────────────────────────────────

ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS score_purchasing_power INTEGER; -- poder de compra real (renda x custo de vida)
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS score_market_gap       INTEGER; -- oferta abaixo do esperado pro segmento
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS score_survival_risk    INTEGER; -- inverso da mortalidade histórica do segmento na cidade

-- ÍNDICES
CREATE INDEX IF NOT EXISTS idx_cities_cost_of_living ON cities(cost_of_living_index);
