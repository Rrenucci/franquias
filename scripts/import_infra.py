"""
import_infra.py
Importa indicadores de infraestrutura domiciliar do Censo 2022 (IBGE/SIDRA)
para as colunas de infraestrutura da tabela `cities` no Supabase.

Complementa o import_ibge.py (que traz população/PIB) — roda depois dele,
já que faz UPDATE nas cidades existentes (match por ibge_code).

Indicadores (todos % de domicílios, Censo 2022, nível município):
  - sanitation_pct       → agregado 6805, esgotamento ligado à rede
  - water access         → agregado 6803, água encanada da rede geral (usado no infrastructure_score)
  - internet_access_pct  → agregado 9936, conexão domiciliar à internet

Uso:
  python scripts/import_infra.py              # importa tudo
  python scripts/import_infra.py --state SP   # filtra por estado
"""

import os
import sys
import argparse

import requests
from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
IBGE_BASE    = "https://servicodados.ibge.gov.br/api/v3/agregados"

BATCH_SIZE = 200

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
             "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

# ─── Indicadores SIDRA (Censo 2022, % de domicílios, nível município) ────────

INDICATORS = {
    "sanitation_pct":      {"agregado": 6805, "classificacao": "11558[46290]",
                             "label": "Esgotamento sanitário adequado"},
    "water_network_pct":   {"agregado": 6803, "classificacao": "1821[72144]",
                             "label": "Água encanada da rede geral"},
    "internet_access_pct": {"agregado": 9936, "classificacao": "2072[77585]",
                             "label": "Acesso à internet"},
}


def fetch_indicator(key: str, cfg: dict) -> dict[str, float]:
    header(f"{cfg['label']} (agregado {cfg['agregado']})")
    url = (f"{IBGE_BASE}/{cfg['agregado']}/periodos/2022/variaveis/1000381"
           f"?localidades=N6[all]&classificacao={cfg['classificacao']}")
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"Falha ao buscar {key}: {e}", "ERR")
        return {}

    result: dict[str, float] = {}
    try:
        for serie in data[0]["resultados"][0]["series"]:
            code  = serie["localidade"]["id"]
            value = serie["serie"].get("2022", "-")
            if value and value not in ("-", "...", "X"):
                result[code] = float(value)
    except (KeyError, IndexError, ValueError, TypeError) as e:
        log(f"Erro ao parsear resposta ({key}): {e}", "WARN")

    log(f"{len(result)} municípios com dado de {key}", "OK")
    return result

# ─── Composição do infrastructure_score ──────────────────────────────────────
# Score simples 0-100: média dos 3 indicadores disponíveis por cidade.
# É um placeholder até termos dado de saúde/comércio (CNES, MUNIC) pra enriquecer.

def compute_infra_score(sanitation: float | None, water: float | None, internet: float | None) -> float | None:
    values = [v for v in (sanitation, water, internet) if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)

# ─── Atualização no Supabase ─────────────────────────────────────────────────

def fetch_all_cities(supabase, state_filter: str | None = None) -> list[dict]:
    """PostgREST limita a 1000 linhas por resposta — pagina com .range() até esgotar."""
    rows: list[dict] = []
    page_size = 1000
    start = 0
    while True:
        query = supabase.table("cities").select("id, ibge_code, state_code").range(start, start + page_size - 1)
        if state_filter:
            query = query.eq("state_code", state_filter.upper())
        batch = query.execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def update_cities(supabase, sanitation, water, internet, state_filter: str | None) -> tuple[int, int]:
    header("Gravando no Supabase (UPDATE por ibge_code)")

    all_codes = set(sanitation) | set(water) | set(internet)
    log(f"{len(all_codes)} municípios com pelo menos um indicador", "INFO")

    existing = fetch_all_cities(supabase, state_filter)
    log(f"{len(existing)} cidades já cadastradas na tabela cities", "INFO")

    updated, skipped = 0, 0
    for city in existing:
        code = city["ibge_code"]
        s, w, i = sanitation.get(code), water.get(code), internet.get(code)
        if s is None and w is None and i is None:
            skipped += 1
            continue

        payload = {
            "sanitation_pct":      s,
            "internet_access_pct": i,
            "infrastructure_score": compute_infra_score(s, w, i),
        }
        try:
            supabase.table("cities").update(payload).eq("ibge_code", code).execute()
            updated += 1
        except Exception as e:
            log(f"Erro ao atualizar {code}: {e}", "ERR")

    log(f"{updated} cidades atualizadas | {skipped} sem dado novo", "OK")
    return updated, skipped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Importa infraestrutura domiciliar (Censo 2022) para o Supabase")
    p.add_argument("--state", type=str, default=None, help="Filtra por estado, ex: --state SP")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n{BOLD}{'='*60}")
    print("  Franchise Intelligence — Importador de Infraestrutura")
    print(f"{'='*60}{RESET}\n")

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    log("Conectado ao Supabase", "OK")

    sanitation = fetch_indicator("sanitation_pct", INDICATORS["sanitation_pct"])
    water      = fetch_indicator("water_network_pct", INDICATORS["water_network_pct"])
    internet   = fetch_indicator("internet_access_pct", INDICATORS["internet_access_pct"])

    updated, skipped = update_cities(supabase, sanitation, water, internet, args.state)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído — {updated} cidades atualizadas, {skipped} sem dado novo")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
