"""
recommend.py
Motor de recomendação — recebe quanto o investidor tem e em qual cidade
quer investir, e devolve as franquias mais indicadas, com score explicado.

Grava a busca em `searches` e o resultado em `recommendations` (histórico).

── Eixos de score implementados nesta v1 (0-100 cada) ────────────────────
  score_investment_fit   — o investimento cabe na faixa da franquia
  score_city_demand      — tamanho da cidade (população) como proxy de mercado
  score_purchasing_power — renda da cidade vs. média do estado (import_income.py)

── Eixos AINDA NÃO calculados (ficam NULL) ───────────────────────────────
  score_city_growth   — precisa de population_growth_rate, que nenhum
                         importador preenche ainda (campo existe no schema,
                         mas sem fonte de dado plugada)
  score_competition    ─┐
  score_market_gap      │ dependem de `city_segments` (densidade de empresas
  score_survival_risk  ─┘ por segmento) e `company_lifecycle` (mortalidade de
                         CNPJ) — nenhuma das duas tabelas tem dado ainda.
                         Ver scripts/import_cnpj_lifecycle.py.

score_total é a média dos eixos que TÊM dado (não penaliza por eixo
faltando — evita que toda recomendação pareça ruim só porque a parte de
CNPJ ainda não foi implementada).

Uso:
  python scripts/recommend.py --investment 150000 --city "Conselheiro Lafaiete" --state MG
  python scripts/recommend.py --investment 80000 --city "Belo Horizonte" --state MG --segment alimentacao --top 5
"""

import os
import sys
import argparse

from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

CYAN, GREEN, YELLOW, RED, RESET, BOLD, DIM = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m", "\033[2m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

# ─── Busca da cidade ──────────────────────────────────────────────────────

def find_city(supabase, city_name: str, state: str | None) -> dict | None:
    query = supabase.table("cities").select("*").ilike("name", f"%{city_name}%")
    if state:
        query = query.eq("state_code", state.upper())
    rows = query.limit(5).execute().data
    if not rows:
        return None
    if len(rows) > 1 and not state:
        log(f"{len(rows)} cidades encontradas com esse nome, usando a primeira. Use --state pra desambiguar.", "WARN")
        for r in rows:
            log(f"    {r['name']} ({r['state_code']})", "INFO")
    return rows[0]

# ─── Scores ───────────────────────────────────────────────────────────────

def score_investment_fit(budget: int, inv_min: int, inv_max: int) -> float:
    if inv_min <= budget <= inv_max:
        return 100.0
    if budget < inv_min:
        # quanto mais longe do mínimo, pior — zera se precisar do dobro do que tem
        gap_ratio = (inv_min - budget) / inv_min
        return max(0.0, 100.0 * (1 - gap_ratio))
    # budget > inv_max: sobra caixa, não é defeito, mas não é o uso mais eficiente do capital
    excess_ratio = (budget - inv_max) / inv_max
    return max(60.0, 100.0 - excess_ratio * 20)


def score_city_demand(population: int | None) -> int | None:
    if not population:
        return None
    # escala log: 5 mil hab. ~ 20 pontos, 100 mil ~ 60 pontos, 1 milhão+ ~ 100 pontos
    import math
    score = (math.log10(max(population, 1)) - 3) * 25
    return int(round(max(0.0, min(100.0, score))))


def score_purchasing_power(ppi: float | None) -> int | None:
    if ppi is None:
        return None
    # ppi 100 = média do estado. Mapeia 50->25, 100->50, 150->75, 200->100
    return int(round(max(0.0, min(100.0, ppi / 2))))


def fetch_survival_by_segment(supabase, city_id: str) -> dict[str, int]:
    """
    {segment: score_survival_risk} pra uma cidade, a partir de company_lifecycle.
    Mortalidade em 3 anos vira "risco" invertido: 100 = ninguém fechou,
    0 = todo mundo fechou rápido. Pondera pela quantidade de empresas
    (opened_count) de cada CNAE — um CNAE com 2 empresas não deveria pesar
    igual a um com 500 na média do segmento.
    """
    rows = supabase.table("company_lifecycle").select("segment, opened_count, mortality_rate_3y") \
        .eq("city_id", city_id).not_.is_("mortality_rate_3y", "null").execute().data

    by_segment: dict[str, list[tuple[float, int]]] = {}
    for r in rows:
        seg = r.get("segment")
        if not seg or r.get("mortality_rate_3y") is None:
            continue
        by_segment.setdefault(seg, []).append((r["mortality_rate_3y"], r.get("opened_count") or 1))

    scores = {}
    for seg, pairs in by_segment.items():
        total_weight = sum(w for _, w in pairs)
        if not total_weight:
            continue
        weighted_mortality = sum(rate * w for rate, w in pairs) / total_weight
        scores[seg] = int(round(max(0.0, min(100.0, 100 - weighted_mortality))))
    return scores


def compute_scores(city: dict, franchise: dict, budget: int, survival_by_segment: dict[str, int]) -> dict:
    s_invest = score_investment_fit(budget, franchise["investment_min"], franchise["investment_max"])
    s_demand = score_city_demand(city.get("population"))
    s_power  = score_purchasing_power(city.get("purchasing_power_index"))
    s_survival = survival_by_segment.get(franchise["segment"])

    available = [s for s in (s_invest, s_demand, s_power, s_survival) if s is not None]
    total = round(sum(available) / len(available), 1) if available else 0.0

    return {
        "score_investment_fit": int(round(s_invest)),
        "score_city_demand":    s_demand,
        "score_purchasing_power": s_power,
        "score_survival_risk":  s_survival,
        "score_city_growth":    None,  # sem fonte de dado ainda (population_growth_rate não é preenchido por nenhum importador)
        "score_competition":    None,  # depende de city_segments, que continua vazio (diferente de company_lifecycle)
        "score_market_gap":     None,  # idem
        "score_total":          int(round(total)),
    }

# ─── Recomendação ─────────────────────────────────────────────────────────

def recommend(supabase, city: dict, budget: int, segment: str | None, top_n: int) -> list[dict]:
    query = supabase.table("franchises").select("*").eq("active", True)
    if segment:
        query = query.eq("segment", segment)
    franchises = query.execute().data

    if not franchises:
        return []

    survival_by_segment = fetch_survival_by_segment(supabase, city["id"])

    results = []
    for f in franchises:
        scores = compute_scores(city, f, budget, survival_by_segment)
        results.append({**f, **scores})

    results.sort(key=lambda r: r["score_total"], reverse=True)
    return results[:top_n]


def persist_search(supabase, city: dict, budget: int, segment: str | None, results: list[dict]) -> None:
    search_row = {
        "city_id": city["id"],
        "investment_amount": budget,
        "segments_filter": [segment] if segment else None,
        "results_count": len(results),
    }
    search = supabase.table("searches").insert(search_row).execute().data[0]

    if not results:
        return

    rec_rows = [{
        "search_id": search["id"],
        "franchise_id": r["id"],
        "rank_position": i + 1,
        "score_total": r["score_total"],
        "score_investment_fit": r["score_investment_fit"],
        "score_city_demand": r["score_city_demand"],
        "score_competition": r["score_competition"],
        "score_city_growth": r["score_city_growth"],
        "score_purchasing_power": r["score_purchasing_power"],
        "score_market_gap": r["score_market_gap"],
        "score_survival_risk": r["score_survival_risk"],
    } for i, r in enumerate(results)]
    supabase.table("recommendations").insert(rec_rows).execute()

# ─── Apresentação ─────────────────────────────────────────────────────────

def print_results(city: dict, budget: int, results: list[dict]) -> None:
    header(f"Recomendações para {city['name']} ({city['state_code']}) — R$ {budget:,}".replace(",", "."))

    if not results:
        log("Nenhuma franquia encontrada com esses filtros.", "WARN")
        return

    for i, r in enumerate(results, 1):
        inv = f"R$ {r['investment_min']:,}-{r['investment_max']:,}".replace(",", ".")
        print(f"\n  {BOLD}{i}. {r['name']}{RESET}  {DIM}({r['segment']}){RESET}  —  score {GREEN}{r['score_total']}{RESET}/100")
        print(f"     Investimento: {inv}")
        breakdown = []
        for label, key in [("fit investimento", "score_investment_fit"),
                            ("demanda cidade", "score_city_demand"),
                            ("poder de compra", "score_purchasing_power"),
                            ("sobrevida do segmento", "score_survival_risk")]:
            v = r[key]
            breakdown.append(f"{label}={v}" if v is not None else f"{label}=n/d")
        print(f"     {DIM}{' | '.join(breakdown)}{RESET}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recomenda franquias por cidade e orçamento de investimento")
    p.add_argument("--investment", type=int, required=True, help="Quanto o investidor tem disponível, em R$")
    p.add_argument("--city", type=str, required=True, help="Nome da cidade")
    p.add_argument("--state", type=str, default=None, help="Sigla do estado, ex: MG (desambigua cidades homônimas)")
    p.add_argument("--segment", type=str, default=None, help="Filtra por segmento (ex: alimentacao)")
    p.add_argument("--top", type=int, default=10, help="Quantas franquias retornar (default 10)")
    p.add_argument("--no-save", action="store_true", help="Não grava a busca em searches/recommendations")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    city = find_city(supabase, args.city, args.state)
    if not city:
        log(f"Cidade '{args.city}' não encontrada.", "ERR")
        sys.exit(1)

    results = recommend(supabase, city, args.investment, args.segment, args.top)
    print_results(city, args.investment, results)

    if not args.no_save:
        persist_search(supabase, city, args.investment, args.segment, results)
        log("\nBusca salva em searches/recommendations", "OK")


if __name__ == "__main__":
    main()
