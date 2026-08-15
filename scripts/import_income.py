"""
import_income.py
Importa renda domiciliar per capita (Censo 2022/SIDRA) e calcula o índice
de poder de compra relativo de cada cidade dentro do seu estado.

── Metodologia (por que é assim) ─────────────────────────────────────────
O que o IBGE PUBLICA por município (Censo 2022, agregado 10295):
  - rendimento domiciliar per capita médio      → avg_ticket_estimate
  - rendimento domiciliar per capita mediano     → median_income
    (mediana é mais resistente a outliers que a média — bairro nobre não
    "puxa" o número da cidade toda)

O que o IBGE NÃO publica por município: um índice de custo de vida.
IPCA só existe em nível nacional (agregado 1737, nivelTerritorial=N1) — não
tem quebra por município nem por região metropolitana num agregado aberto
verificado até agora. Por isso `cost_of_living_index` fica de propósito
sem fonte aqui — é melhor deixar NULL do que inventar um número que entra
numa decisão de investimento real. Ver TODO no fim do arquivo.

Como proxy honesto de poder de compra RELATIVO (que dá pra calcular pra
todo município, com dado real), usamos:

    purchasing_power_index = (renda_per_capita_média_cidade
                               / renda_per_capita_média_do_estado) * 100

100 = a cidade tem a renda média do seu próprio estado. Acima de 100 =
poder de compra acima da média estadual. É relativo, não absoluto — não
diz se R$ 2.000 dá pra viver bem em Conselheiro Lafaiete, mas diz se
aquela cidade é rica ou pobre *pro padrão de Minas Gerais*, que é o
recorte que importa pra decidir entre duas cidades do mesmo estado.

Fonte: agregado SIDRA 10295, variáveis 13431 (média) e 13534 (mediana),
classificação "Total" (sexo/cor/idade), nível N6 (município) e N3 (estado).
Testado contra a API real (São Paulo cidade: R$ 2.713,36 médio / estado SP:
R$ 2.093,44 médio, Censo 2022) antes de escrever este script.

Uso:
  python scripts/import_income.py
  python scripts/import_income.py --state SP
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

AGREGADO = 10295
VAR_MEDIA   = 13431
VAR_MEDIANA = 13534
CLASSIF = "2[6794]|86[95251]|58[95253]"  # sexo=Total, cor=Total, idade=Total

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


def fetch_income(nivel: str, codigo: str = "all") -> dict[str, tuple[float, float]]:
    """Retorna {codigo_localidade: (media, mediana)}."""
    url = (f"{IBGE_BASE}/{AGREGADO}/periodos/2022/variaveis/{VAR_MEDIA}|{VAR_MEDIANA}"
           f"?localidades={nivel}[{codigo}]&classificacao={CLASSIF}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    data = r.json()

    media_by_loc: dict[str, float] = {}
    mediana_by_loc: dict[str, float] = {}
    for bloco in data:
        var_id = bloco["id"]
        for resultado in bloco["resultados"]:
            for serie in resultado["series"]:
                code = serie["localidade"]["id"]
                value = serie["serie"].get("2022", "-")
                if value in ("-", "...", "X", None):
                    continue
                (media_by_loc if var_id == str(VAR_MEDIA) else mediana_by_loc)[code] = float(value)

    return {code: (media_by_loc.get(code), mediana_by_loc.get(code))
            for code in set(media_by_loc) | set(mediana_by_loc)}


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


def update_cities(supabase, city_income: dict, state_avg: dict[str, float], state_filter: str | None) -> tuple[int, int]:
    header("Gravando no Supabase (UPDATE por ibge_code)")

    existing = fetch_all_cities(supabase, state_filter)
    log(f"{len(existing)} cidades cadastradas", "INFO")

    updated, skipped = 0, 0
    for city in existing:
        code = city["ibge_code"]
        if code not in city_income:
            skipped += 1
            continue

        media, mediana = city_income[code]
        state_media = state_avg.get(city["state_code"])
        ppi = round((media / state_media) * 100, 2) if media and state_media else None

        payload = {
            "avg_ticket_estimate":    media,
            "median_income":          mediana,
            "purchasing_power_index": ppi,
        }
        try:
            supabase.table("cities").update(payload).eq("ibge_code", code).execute()
            updated += 1
        except Exception as e:
            log(f"Erro ao atualizar {code}: {e}", "ERR")

    log(f"{updated} cidades atualizadas | {skipped} sem dado", "OK")
    return updated, skipped


# Código IBGE de UF (N3) por sigla — necessário pra buscar a média estadual
UF_CODES = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27,
    "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35, "PR": 41,
    "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Importa renda per capita (Censo 2022) e calcula índice de poder de compra relativo")
    p.add_argument("--state", type=str, default=None, help="Filtra por estado, ex: --state SP")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n{BOLD}{'='*60}")
    print("  Franchise Intelligence — Importador de Renda / Poder de Compra")
    print(f"{'='*60}{RESET}\n")

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    log("Conectado ao Supabase", "OK")

    header("Renda por município (Censo 2022)")
    if args.state:
        # Municípios não têm um filtro direto por UF na API de agregados —
        # busca-se tudo e filtra depois na gravação (igual import_infra.py).
        log("Nota: --state filtra na gravação, a busca ainda é nacional", "INFO")
    city_income = fetch_income("N6")
    log(f"{len(city_income)} municípios com dado de renda", "OK")

    header("Renda média por estado (Censo 2022)")
    ufs = [args.state.upper()] if args.state else list(UF_CODES)
    state_avg: dict[str, float] = {}
    for uf in ufs:
        result = fetch_income("N3", str(UF_CODES[uf]))
        media, _ = result.get(str(UF_CODES[uf]), (None, None))
        if media:
            state_avg[uf] = media
            log(f"{uf}: R$ {media:.2f}")
    log(f"{len(state_avg)} estados com renda média calculada", "OK")

    updated, skipped = update_cities(supabase, city_income, state_avg, args.state)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído — {updated} cidades atualizadas, {skipped} sem dado")
    print(f"  cost_of_living_index NÃO foi preenchido (sem fonte municipal confiável — ver TODO)")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()

# TODO: cost_of_living_index. IPCA do IBGE só tem série nacional num
# agregado aberto verificado (1737, nivelTerritorial N1). Existem índices
# de custo de vida por região metropolitana em fontes fechadas/pagas
# (ex: FipeZAP pra aluguel). Antes de preencher essa coluna, validar se
# existe mesmo uma fonte pública municipal — não estimar/inventar o
# número, já que ele entraria direto numa decisão de investimento real.
