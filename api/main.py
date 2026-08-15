"""
api/main.py
API HTTP do Franchise Intelligence — expõe o motor de recomendação
(scripts/recommend.py) e serve o frontend estático.

Uso:
  python -m uvicorn api.main:app --reload --port 8000
Depois abra http://localhost:8000
"""

import os
import sys

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from supabase import create_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import recommend as rec  # noqa: E402  (path precisa ser ajustado antes do import)

SUPABASE_URL = rec.SUPABASE_URL
SUPABASE_KEY = rec.SUPABASE_KEY

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY ausentes no .env")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = FastAPI(title="Franchise Intelligence API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Rótulo legível pra cada segmento (os slugs vêm de scripts/import_franchises.py)
SEGMENT_LABELS = {
    "alimentacao": "Alimentação",
    "casa_e_construcao": "Casa e Construção",
    "educacao": "Educação",
    "comunicacao_e_tecnologia": "Comunicação, Informática e Eletrônicos",
    "entretenimento_e_lazer": "Entretenimento e Lazer",
    "hotelaria_e_turismo": "Hotelaria e Turismo",
    "limpeza_e_conservacao": "Limpeza e Conservação",
    "moda": "Moda",
    "saude_beleza_bem_estar": "Saúde, Beleza e Bem-Estar",
    "servicos_automotivos": "Serviços Automotivos",
    "servicos_e_outros_negocios": "Serviços e Outros Negócios",
}


class RecommendRequest(BaseModel):
    city_id: str
    investment: int
    segment: str | None = None
    top: int = 10
    save: bool = True


@app.get("/api/segments")
def list_segments():
    return [{"value": v, "label": l} for v, l in SEGMENT_LABELS.items()]


@app.get("/api/cities")
def search_cities(q: str):
    if len(q.strip()) < 2:
        return []
    rows = (
        supabase.table("cities")
        .select("id, name, state_code, population")
        .ilike("name", f"%{q.strip()}%")
        .order("population", desc=True)
        .limit(8)
        .execute()
        .data
    )
    return rows


@app.post("/api/recommend")
def recommend_endpoint(req: RecommendRequest):
    city_row = supabase.table("cities").select("*").eq("id", req.city_id).limit(1).execute().data
    if not city_row:
        raise HTTPException(status_code=404, detail="Cidade não encontrada")
    city = city_row[0]

    if req.investment <= 0:
        raise HTTPException(status_code=400, detail="Investimento precisa ser maior que zero")

    results = rec.recommend(supabase, city, req.investment, req.segment, min(req.top, 50))

    if req.save:
        rec.persist_search(supabase, city, req.investment, req.segment, results)

    return {
        "city": {
            "id": city["id"],
            "name": city["name"],
            "state_code": city["state_code"],
            "population": city.get("population"),
        },
        "investment": req.investment,
        "results": [{
            "name": r["name"],
            "slug": r["slug"],
            "segment": r["segment"],
            "segment_label": SEGMENT_LABELS.get(r["segment"], r["segment"]),
            "logo_url": r.get("logo_url"),
            "brand_url": r.get("brand_url"),
            "investment_min": r["investment_min"],
            "investment_max": r["investment_max"],
            "royalty_pct": r.get("royalty_pct"),
            "payback_months_min": r.get("payback_months_min"),
            "payback_months_max": r.get("payback_months_max"),
            "units_count": r.get("units_count"),
            "score_total": r["score_total"],
            "score_investment_fit": r["score_investment_fit"],
            "score_city_demand": r["score_city_demand"],
            "score_purchasing_power": r["score_purchasing_power"],
            "score_survival_risk": r["score_survival_risk"],
            "score_market_gap": r["score_market_gap"],
            "matched_model": r.get("matched_model"),
            "matched_model_investment_min": r.get("matched_model_investment_min"),
            "matched_model_investment_max": r.get("matched_model_investment_max"),
        } for r in results],
    }


@app.get("/api/franchise/{slug}")
def franchise_detail(slug: str):
    rows = supabase.table("franchises").select("*").eq("slug", slug).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail="Franquia não encontrada")
    f = rows[0]
    return {
        "name": f["name"],
        "slug": f["slug"],
        "segment": f["segment"],
        "segment_label": SEGMENT_LABELS.get(f["segment"], f["segment"]),
        "logo_url": f.get("logo_url"),
        "brand_url": f.get("brand_url"),
        "investment_min": f["investment_min"],
        "investment_max": f["investment_max"],
        "royalty_pct": f.get("royalty_pct"),
        "payback_months_min": f.get("payback_months_min"),
        "payback_months_max": f.get("payback_months_max"),
        "units_count": f.get("units_count"),
        "investment_models": f.get("investment_models"),
        "investment_models_fetched_at": f.get("investment_models_fetched_at"),
        "faq_data": f.get("faq_data"),
        "faq_fetched_at": f.get("faq_fetched_at"),
    }

# ─── Chat — explica as recomendações, respondendo perguntas livres ──────────
# Grounded no dado real: o system prompt carrega os números de verdade (cidade
# + scores + payback/royalty/FAQ de cada franquia), e a instrução é pra Claude
# nunca inventar número que não esteja ali.

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    city_id: str
    investment: int
    segment: str | None = None
    question: str
    history: list[ChatMessage] = []


def brl(value) -> str:
    if value is None:
        return "sem dado"
    return f"R$ {value:,.0f}".replace(",", ".")


def build_grounding_context(city: dict, results: list[dict]) -> str:
    lines = [
        f"CIDADE: {city['name']} ({city['state_code']})",
        f"- População: {city.get('population') or 'sem dado'}",
        f"- Renda média per capita: R$ {city.get('avg_ticket_estimate')}" if city.get("avg_ticket_estimate") else "- Renda média per capita: sem dado",
        f"- Poder de compra relativo ao estado (100 = média do estado): {city.get('purchasing_power_index')}" if city.get("purchasing_power_index") else "- Poder de compra relativo ao estado: sem dado",
        f"- Saneamento adequado: {city.get('sanitation_pct')}%" if city.get("sanitation_pct") else "- Saneamento adequado: sem dado",
        f"- Acesso à internet: {city.get('internet_access_pct')}%" if city.get("internet_access_pct") else "- Acesso à internet: sem dado",
        f"- PIB per capita: R$ {city.get('gdp_per_capita')}" if city.get("gdp_per_capita") else "- PIB per capita: sem dado",
        "",
        f"FRANQUIAS RECOMENDADAS PARA ESSA CIDADE (ordenadas por score, {len(results)} no total):",
    ]

    franchise_ids = [r["id"] for r in results]
    extra_by_id = {}
    if franchise_ids:
        extra_rows = (
            supabase.table("franchises")
            .select("id, faq_data, investment_models")
            .in_("id", franchise_ids)
            .execute()
            .data
        )
        extra_by_id = {row["id"]: row for row in extra_rows}

    for i, r in enumerate(results, 1):
        if r.get("matched_model"):
            inv = f"{brl(r['matched_model_investment_min'])} a {brl(r['matched_model_investment_max'])} (modelo \"{r['matched_model']}\", o que melhor se encaixa no orçamento informado)"
        else:
            inv = f"R$ {r['investment_min']:,} - R$ {r['investment_max']:,} (faixa geral do catálogo, sem detalhe por modelo)".replace(",", ".")
        lines.append(f"\n{i}. {r['name']} — segmento {SEGMENT_LABELS.get(r['segment'], r['segment'])} — score total {r['score_total']}/100")
        lines.append(f"   Investimento: {inv}")
        lines.append(
            f"   Scores (0-100): fit de investimento={r['score_investment_fit']}, "
            f"demanda da cidade={r['score_city_demand']}, poder de compra={r['score_purchasing_power']}, "
            f"sobrevida do segmento (mortalidade real de CNPJ na cidade)={r['score_survival_risk']}, "
            f"gap de mercado (densidade de empresas ATIVAS do segmento na cidade vs. mediana nacional do "
            f"segmento — mede oferta do SEGMENTO, não da marca; não sabemos se essa franquia específica já "
            f"tem unidade aqui)={r['score_market_gap']}"
        )

        extra = extra_by_id.get(r["id"], {})
        models = extra.get("investment_models")
        faq = extra.get("faq_data")

        if models:
            lines.append("   [Dados de investimento por modelo, declarados pela franqueadora à ABF — NÃO auditados por nós; trate como piso de checagem, não como número garantido]")
            for m in models:
                lines.append(f"     Modelo \"{m.get('model')}\":")
                if m.get("total_investment_min") is not None:
                    lines.append(f"       Investimento total: {brl(m['total_investment_min'])} a {brl(m['total_investment_max'])}")
                if m.get("franchise_fee") is not None:
                    lines.append(f"       Taxa de franquia: {brl(m['franchise_fee'])}")
                if m.get("payback_months_min") is not None:
                    lines.append(f"       Payback (retorno do investimento): {m['payback_months_min']} a {m['payback_months_max']} meses")
                if m.get("avg_monthly_revenue") is not None:
                    lines.append(f"       Faturamento médio mensal declarado: {brl(m['avg_monthly_revenue'])}")
                royalty = m.get("royalty_fee") or {}
                if royalty.get("type") == "percent":
                    lines.append(f"       Royalties: {royalty['value']}% (base: {royalty.get('base', 'não especificada')})")
                elif royalty.get("type") == "fixed_brl":
                    lines.append(f"       Royalties: {brl(royalty['value'])} fixo (base: {royalty.get('base', 'não especificada')})")
                elif royalty.get("type") == "none":
                    lines.append("       Royalties: não cobra")
                marketing = m.get("marketing_fee") or {}
                if marketing.get("type") == "percent":
                    lines.append(f"       Taxa de propaganda: {marketing['value']}% (base: {marketing.get('base', 'não especificada')})")
                elif marketing.get("type") == "fixed_brl":
                    lines.append(f"       Taxa de propaganda: {brl(marketing['value'])} fixo (base: {marketing.get('base', 'não especificada')})")
                elif marketing.get("type") == "none":
                    lines.append("       Taxa de propaganda: não cobra")
                if m.get("units_count") is not None:
                    lines.append(f"       Unidades em operação (nesse modelo): {m['units_count']}")
                if m.get("hq_state"):
                    lines.append(f"       Sede da franqueadora: {m['hq_state']}")

        if faq:
            relevant = [q for q in faq if any(k in q["question"].lower() for k in
                        ["lucratividade", "margem"])]
            for q in relevant[:2]:
                lines.append(f"   [Dado oficial da franquia, FAQ] {q['question']}: {q['answer']}")

        if not models and not faq:
            lines.append("   [Sem dado de payback/royalties/faturamento coletado para esta franquia ainda — não estime, diga que não foi coletado]")

    return "\n".join(lines)


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    if claude is None:
        raise HTTPException(status_code=503, detail="Chat indisponível: ANTHROPIC_API_KEY não configurada no servidor")

    city_row = supabase.table("cities").select("*").eq("id", req.city_id).limit(1).execute().data
    if not city_row:
        raise HTTPException(status_code=404, detail="Cidade não encontrada")
    city = city_row[0]

    results = rec.recommend(supabase, city, req.investment, req.segment, 12)
    context = build_grounding_context(city, results)

    system_prompt = (
        "Você é um consultor de dados do Franchise Intelligence, ajudando um investidor a entender "
        "recomendações de franquias para uma cidade específica. Responda SOMENTE com base nos dados "
        "fornecidos abaixo — nunca invente número, taxa, prazo ou fato que não esteja explicitamente "
        "ali. Se o dado que a pessoa pediu não estiver disponível, diga claramente que não foi coletado "
        "ainda, em vez de estimar. Seja direto e cite os números reais ao explicar o porquê de uma "
        "recomendação. Não temos dado de presença por MARCA na cidade (se essa franquia específica já "
        "tem unidade ali) — se perguntarem isso, diga que não é possível verificar com os dados "
        "disponíveis e recomende que o investidor confirme direto com a franqueadora. O 'gap de mercado' "
        "mede o segmento como um todo, nunca trate como se fosse sobre a marca específica. Responda em "
        "português do Brasil.\n\n" + context
    )

    messages = [{"role": m.role, "content": m.content} for m in req.history]
    messages.append({"role": "user", "content": req.question})

    try:
        response = claude.beta.messages.create(
            model="claude-opus-5",
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
            thinking={"type": "adaptive"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar a IA: {e.message}")

    if response.stop_reason == "refusal":
        raise HTTPException(status_code=422, detail="A IA não conseguiu responder essa pergunta.")

    answer = next((b.text for b in response.content if b.type == "text"), "")
    return {"answer": answer}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
