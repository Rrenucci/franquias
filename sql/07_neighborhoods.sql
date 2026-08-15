-- Renda por bairro (nomeado) dentro de cada cidade — Censo 2022, produto
-- "Agregados por Setores Censitários - Rendimento do Responsável" do IBGE.
-- Pequeno (17.379 bairros no Brasil todo), sem malha geográfica — dá pra
-- rankear bairros de uma cidade por renda, não pra geocodificar um endereço
-- específico (isso precisaria da malha de setores censitários, um projeto
-- à parte).

CREATE TABLE IF NOT EXISTS neighborhoods (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id             UUID REFERENCES cities(id) ON DELETE CASCADE,
    cd_bairro           VARCHAR(12) UNIQUE,
    name                TEXT NOT NULL,
    households_count    INTEGER,
    residents_count     INTEGER,
    avg_income          NUMERIC,
    median_income       NUMERIC,
    data_year           INTEGER DEFAULT 2022,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_neighborhoods_city ON neighborhoods(city_id);
CREATE INDEX IF NOT EXISTS idx_neighborhoods_income ON neighborhoods(median_income);
