"""
compute_segment_benchmarks.py
Calcula, pra cada segmento, a densidade "típica" de empresas ativas por
10 mil habitantes — usada como referência pro score_market_gap (a cidade
escolhida tem MENOS empresas desse segmento do que o esperado pro tamanho
dela, ou já está saturada?).

100% derivado de dado já coletado (company_lifecycle, que veio da Receita
Federal via scripts/import_cnpj_lifecycle.py) — nenhuma ingestão nova.

Uso:
  python scripts/compute_segment_benchmarks.py
"""

import os
import sys
import statistics
from collections import defaultdict

from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

CYAN, GREEN, YELLOW, RESET, BOLD = "\033[96m", "\033[92m", "\033[93m", "\033[0m", "\033[1m"


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}", "WARN": f"{YELLOW}⚠{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def fetch_all(supabase, table: str, columns: str, page_size: int = 1000) -> list[dict]:
    rows, start = [], 0
    while True:
        batch = supabase.table(table).select(columns).range(start, start + page_size - 1).execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def main() -> None:
    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "WARN")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print(f"\n{BOLD}{CYAN}── Carregando company_lifecycle + cities ──{RESET}")
    lifecycle = fetch_all(supabase, "company_lifecycle", "city_id, segment, active_count")
    cities = fetch_all(supabase, "cities", "id, population")
    pop_by_city = {c["id"]: c["population"] for c in cities if c.get("population")}
    log(f"{len(lifecycle)} linhas de company_lifecycle, {len(pop_by_city)} cidades com população", "INFO")

    # soma active_count por (city, segment) — um segmento tem várias CNAEs
    active_by_city_segment: dict[tuple[str, str], int] = defaultdict(int)
    for r in lifecycle:
        city_id, seg, active = r.get("city_id"), r.get("segment"), r.get("active_count")
        if not city_id or not seg or active is None:
            continue
        active_by_city_segment[(city_id, seg)] += active

    # densidade (empresas ativas por 10k hab.) por cidade, agrupado por segmento
    density_by_segment: dict[str, list[float]] = defaultdict(list)
    for (city_id, seg), active in active_by_city_segment.items():
        population = pop_by_city.get(city_id)
        if not population:
            continue
        density_by_segment[seg].append(active / population * 10000)

    print(f"\n{BOLD}{CYAN}── Calculando benchmark por segmento ──{RESET}")
    rows = []
    for seg, densities in sorted(density_by_segment.items()):
        avg = statistics.mean(densities)
        median = statistics.median(densities)
        rows.append({
            "segment": seg,
            "avg_active_per_10k": round(avg, 4),
            "median_active_per_10k": round(median, 4),
            "cities_sampled": len(densities),
            "updated_at": "now()",
        })
        log(f"{seg}: média={avg:.2f} mediana={median:.2f} /10k hab. ({len(densities)} cidades)", "OK")

    supabase.table("segment_density_benchmark").upsert(rows, on_conflict="segment").execute()
    print(f"\n{BOLD}{GREEN}Concluído — {len(rows)} segmentos gravados em segment_density_benchmark{RESET}\n")


if __name__ == "__main__":
    main()
