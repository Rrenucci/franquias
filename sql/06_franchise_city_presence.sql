-- Presença de marca por cidade — a franquia ESPECÍFICA (não o segmento) já
-- opera ou já operou naquela cidade, detectado via nome_fantasia da Receita
-- Federal (scripts/detect_brand_presence.py). Ausência de linha aqui NÃO
-- significa ausência da franquia — só que não achamos evidência (nome
-- fantasia em branco é comum nos dados da Receita).

CREATE TABLE IF NOT EXISTS franchise_city_presence (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    franchise_id          UUID REFERENCES franchises(id) ON DELETE CASCADE,
    city_id               UUID REFERENCES cities(id) ON DELETE CASCADE,
    active_units_matched  INTEGER DEFAULT 0,
    closed_units_matched  INTEGER DEFAULT 0,
    sample_nome_fantasia  TEXT,
    data_reference_date   DATE,
    updated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(franchise_id, city_id)
);

CREATE INDEX IF NOT EXISTS idx_presence_city ON franchise_city_presence(city_id);
