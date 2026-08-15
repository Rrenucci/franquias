-- Dados oficiais de investimento por modelo (Loja, Home Based, Quiosque etc.),
-- extraídos da tabela estruturada que a própria ABF publica na página de cada
-- franquia — não é o bloco FAQPage (schema.org), é uma tabela HTML separada
-- com granularidade maior: payback, royalties (tipo + valor + base de
-- cálculo), taxa de propaganda, faturamento médio mensal, unidades, sede.
--
-- Isso preenche o buraco que o chat detectou sozinho: franquias sem FAQPage
-- (a maioria) ainda tinham esses dados publicados nessa outra estrutura.

ALTER TABLE franchises ADD COLUMN IF NOT EXISTS investment_models JSONB;
ALTER TABLE franchises ADD COLUMN IF NOT EXISTS investment_models_fetched_at TIMESTAMPTZ;
