-- ============================================================
-- Franchise Intelligence — Schema v0.1
-- Execute no Supabase SQL Editor antes de rodar o import
-- ============================================================

-- CIDADES (dados IBGE)
CREATE TABLE IF NOT EXISTS cities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ibge_code       VARCHAR(7) UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    state_code      CHAR(2) NOT NULL,
    state_name      TEXT NOT NULL,
    region          TEXT NOT NULL,
    mesoregion      TEXT,
    microregion     TEXT,

    -- Demográfico
    population          INTEGER,
    population_growth_rate NUMERIC(6,4),
    urban_population_pct   NUMERIC(5,2),

    -- Econômico
    gdp_per_capita  NUMERIC(12,2),
    gdp_total       BIGINT,
    idh             NUMERIC(5,3),
    median_income   NUMERIC(10,2),

    -- Educação
    literacy_rate   NUMERIC(5,2),
    higher_edu_pct  NUMERIC(5,2),

    -- Geográfico
    area_km2        NUMERIC(10,2),
    lat             NUMERIC(10,7),
    lng             NUMERIC(10,7),

    data_year       INTEGER DEFAULT 2022,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- FRANQUIAS
CREATE TABLE IF NOT EXISTS franchises (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    slug            TEXT UNIQUE NOT NULL,
    segment         TEXT NOT NULL,
    subsegment      TEXT,
    brand_url       TEXT,
    logo_url        TEXT,

    investment_min      INTEGER NOT NULL,
    investment_max      INTEGER NOT NULL,
    working_capital     INTEGER,
    royalty_pct         NUMERIC(5,2),
    marketing_pct       NUMERIC(5,2),
    payback_months_min  INTEGER,
    payback_months_max  INTEGER,
    avg_monthly_revenue INTEGER,

    units_count         INTEGER,
    founded_year        INTEGER,
    abf_member          BOOLEAN DEFAULT false,
    requires_store      BOOLEAN DEFAULT true,
    min_area_m2         INTEGER,
    is_home_based       BOOLEAN DEFAULT false,

    active          BOOLEAN DEFAULT true,
    data_source     TEXT DEFAULT 'manual',
    data_updated_at TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- SEGMENTOS POR CIDADE (mapeamento de concorrência via CNPJ)
CREATE TABLE IF NOT EXISTS city_segments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id             UUID REFERENCES cities(id) ON DELETE CASCADE,
    segment             TEXT NOT NULL,
    cnae_codes          TEXT[],
    total_companies     INTEGER DEFAULT 0,
    franchise_companies INTEGER DEFAULT 0,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(city_id, segment)
);

-- BUSCAS (analytics)
CREATE TABLE IF NOT EXISTS searches (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id       TEXT,
    user_id          UUID,
    city_id          UUID REFERENCES cities(id),
    investment_amount INTEGER NOT NULL,
    segments_filter  TEXT[],
    investor_profile TEXT,
    results_count    INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- RECOMENDAÇÕES
CREATE TABLE IF NOT EXISTS recommendations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    search_id       UUID REFERENCES searches(id) ON DELETE CASCADE,
    franchise_id    UUID REFERENCES franchises(id),
    rank_position   INTEGER NOT NULL,
    score_total     INTEGER NOT NULL,

    score_investment_fit  INTEGER,
    score_city_demand     INTEGER,
    score_competition     INTEGER,
    score_city_growth     INTEGER,
    score_explanation     TEXT,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ÍNDICES
CREATE INDEX IF NOT EXISTS idx_cities_state       ON cities(state_code);
CREATE INDEX IF NOT EXISTS idx_cities_ibge        ON cities(ibge_code);
CREATE INDEX IF NOT EXISTS idx_cities_population  ON cities(population DESC);
CREATE INDEX IF NOT EXISTS idx_franchises_segment ON franchises(segment);
CREATE INDEX IF NOT EXISTS idx_franchises_invest  ON franchises(investment_min, investment_max);
CREATE INDEX IF NOT EXISTS idx_city_seg_city      ON city_segments(city_id);
CREATE INDEX IF NOT EXISTS idx_searches_city      ON searches(city_id);
CREATE INDEX IF NOT EXISTS idx_searches_created   ON searches(created_at DESC);

-- Habilitar extensão para busca por similaridade (precisa vir antes do índice trgm abaixo)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Busca textual por nome de cidade
CREATE INDEX IF NOT EXISTS idx_cities_name_trgm ON cities USING gin(name gin_trgm_ops);
