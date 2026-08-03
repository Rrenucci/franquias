"""
import_ibge.py
Importa dados de municípios brasileiros do IBGE para o Supabase.

Fontes:
  - IBGE Localidades API  → nome, estado, região de todos os 5.570 municípios
  - IBGE SIDRA (Censo 22) → população residente por município
  - IBGE SIDRA (PIB 21)   → PIB per capita por município

Uso:
  python scripts/import_ibge.py              # importa tudo
  python scripts/import_ibge.py --only-meta  # só nomes/estados, sem dados econômicos
  python scripts/import_ibge.py --state SP   # filtra por estado
"""

import os
import sys
import time
import argparse
from datetime import datetime

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ─── Configuração ────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
IBGE_BASE    = "https://servicodados.ibge.gov.br/api"

BATCH_SIZE   = 200   # registros por upsert
REQUEST_PAUSE = 1.0  # segundos entre chamadas à API SIDRA (evitar rate limit)

# ─── Cores para terminal ─────────────────────────────────────────────────────

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
             "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {ts} {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")


def progress(current: int, total: int, label: str = "") -> None:
    pct = current / total * 100
    filled = int(pct / 2)
    bar = "█" * filled + "░" * (50 - filled)
    print(f"\r  [{bar}] {pct:5.1f}%  {label}", end="", flush=True)

# ─── HTTP helper ─────────────────────────────────────────────────────────────

def ibge_get(path: str, timeout: int = 90) -> list | dict | None:
    url = f"{IBGE_BASE}{path}"
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.Timeout:
        log(f"Timeout após {timeout}s: {path}", "WARN")
        return None
    except requests.HTTPError as e:
        log(f"HTTP {e.response.status_code}: {path}", "ERR")
        return None
    except Exception as e:
        log(f"Erro inesperado ({path}): {e}", "ERR")
        return None

# ─── Etapa 1: Metadados dos municípios ───────────────────────────────────────

def fetch_municipalities(state_filter: str | None = None) -> list[dict]:
    header("Etapa 1 — Municípios (IBGE Localidades)")
    log("Consultando API de localidades...")

    data = ibge_get("/v1/localidades/municipios?orderBy=nome")
    if not data:
        raise RuntimeError("Falha crítica: não foi possível buscar municípios")

    if state_filter:
        data = [m for m in data if _get_state_code(m) == state_filter.upper()]
        log(f"Filtro aplicado: estado {state_filter.upper()}", "INFO")

    log(f"{len(data)} municípios encontrados", "OK")
    return data


def _get_state_code(m: dict) -> str:
    try:
        return m["microrregiao"]["mesorregiao"]["UF"]["sigla"]
    except (KeyError, TypeError):
        return ""


def parse_municipality(m: dict) -> dict:
    try:
        uf     = m["microrregiao"]["mesorregiao"]["UF"]
        regiao = uf["regiao"]
    except (KeyError, TypeError):
        uf = regiao = {}

    return {
        "ibge_code":   str(m["id"]),
        "name":        m["nome"],
        "state_code":  uf.get("sigla", ""),
        "state_name":  uf.get("nome", ""),
        "region":      regiao.get("nome", ""),
        "mesoregion":  m.get("microrregiao", {}).get("mesorregiao", {}).get("nome"),
        "microregion": m.get("microrregiao", {}).get("nome"),
        "data_year":   2022,
    }

# ─── Etapa 2: População Censo 2022 ───────────────────────────────────────────

def fetch_population() -> dict[str, int]:
    header("Etapa 2 — População (Censo 2022 / SIDRA)")
    log("Consultando SIDRA — agregado 4709, variável 93...")
    log("Esta chamada pode demorar 30–90 segundos.", "WARN")

    # Agregado 4709 = Censo Demográfico 2022
    # Variável  93  = População residente
    data = ibge_get(
        "/v3/agregados/4709/periodos/2022/variaveis/93?localidades=N6[all]",
        timeout=150
    )

    if not data:
        log("Skipping população — continuando com NULL", "WARN")
        return {}

    result: dict[str, int] = {}
    try:
        for serie in data[0]["resultados"][0]["series"]:
            code  = serie["localidade"]["id"]
            value = serie["serie"].get("2022", "-")
            if value and value not in ("-", "...", "X"):
                result[code] = int(value.replace(".", "").replace(",", ""))
    except (KeyError, IndexError, ValueError) as e:
        log(f"Erro ao parsear resposta SIDRA (população): {e}", "WARN")

    log(f"{len(result)} municípios com dados de população", "OK")
    time.sleep(REQUEST_PAUSE)
    return result

# ─── Etapa 3: PIB per capita 2021 ────────────────────────────────────────────

def fetch_gdp_per_capita() -> dict[str, float]:
    header("Etapa 3 — PIB per capita (2021 / SIDRA)")
    log("Consultando SIDRA — agregado 5938, variável 37...")
    log("Esta chamada pode demorar 30–90 segundos.", "WARN")

    # Agregado 5938 = PIB dos Municípios
    # Variável  37  = PIB per capita a preços correntes (R$)
    data = ibge_get(
        "/v3/agregados/5938/periodos/2021/variaveis/37?localidades=N6[all]",
        timeout=150
    )

    if not data:
        log("Skipping PIB — continuando com NULL", "WARN")
        return {}

    result: dict[str, float] = {}
    try:
        for serie in data[0]["resultados"][0]["series"]:
            code  = serie["localidade"]["id"]
            value = serie["serie"].get("2021", "-")
            if value and value not in ("-", "...", "X"):
                cleaned = value.replace(".", "").replace(",", ".")
                result[code] = float(cleaned)
    except (KeyError, IndexError, ValueError) as e:
        log(f"Erro ao parsear resposta SIDRA (PIB): {e}", "WARN")

    log(f"{len(result)} municípios com dados de PIB per capita", "OK")
    return result

# ─── Etapa 4: Upsert no Supabase ─────────────────────────────────────────────

def upsert_cities(
    supabase,
    municipalities: list[dict],
    population: dict[str, int],
    gdp_pc: dict[str, float],
) -> tuple[int, int]:
    header("Etapa 4 — Gravando no Supabase")
    log(f"Inserindo {len(municipalities)} municípios em lotes de {BATCH_SIZE}...")

    total    = len(municipalities)
    inserted = 0
    errors   = 0

    for i in range(0, total, BATCH_SIZE):
        batch_raw = municipalities[i : i + BATCH_SIZE]
        batch     = []

        for m in batch_raw:
            row = parse_municipality(m)
            code = row["ibge_code"]
            row["population"]     = population.get(code)
            row["gdp_per_capita"] = gdp_pc.get(code)
            batch.append(row)

        try:
            supabase.table("cities").upsert(batch, on_conflict="ibge_code").execute()
            inserted += len(batch)
        except Exception as e:
            errors += len(batch)
            log(f"\nErro no lote {i}–{i + BATCH_SIZE}: {e}", "ERR")

        progress(min(i + BATCH_SIZE, total), total, f"{min(i + BATCH_SIZE, total)}/{total} cidades")

    print()  # quebra linha após barra de progresso

    level = "OK" if errors == 0 else "WARN"
    log(f"{inserted} inseridos | {errors} com erro", level)
    return inserted, errors

# ─── Verificação pós-import ───────────────────────────────────────────────────

def verify(supabase) -> None:
    header("Verificação")
    try:
        r = supabase.table("cities").select("state_code", count="exact").execute()
        total = r.count
        log(f"Total de cidades no banco: {total}", "OK")

        # Top 5 estados com mais cidades
        r2 = supabase.rpc("count_cities_by_state").execute()
        if r2.data:
            log("Distribuição por estado (top 5):", "INFO")
            for row in r2.data[:5]:
                print(f"       {row['state_code']:3s}  →  {row['count']:4d} cidades")
    except Exception:
        # RPC pode não existir ainda — sem problema
        log("Banco populado com sucesso", "OK")

# ─── Entry point ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Importa municípios do IBGE para o Supabase")
    p.add_argument("--only-meta", action="store_true",
                   help="Importa só nomes/estados (sem SIDRA — mais rápido)")
    p.add_argument("--state", type=str, default=None,
                   help="Importa apenas um estado, ex: --state SP")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n{BOLD}{'='*60}")
    print("  Franchise Intelligence — Importador IBGE")
    print(f"{'='*60}{RESET}\n")

    # Validação de ambiente
    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        log("Copie .env.example para .env e preencha as chaves do Supabase", "INFO")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    log("Conectado ao Supabase", "OK")

    start = time.time()

    # Coleta de dados
    municipalities = fetch_municipalities(state_filter=args.state)

    population = {}
    gdp_pc     = {}

    if not args.only_meta:
        population = fetch_population()
        gdp_pc     = fetch_gdp_per_capita()
    else:
        log("Modo --only-meta: pulando dados econômicos do SIDRA", "WARN")

    # Gravação
    inserted, errors = upsert_cities(supabase, municipalities, population, gdp_pc)

    # Verificação
    verify(supabase)

    # Resumo final
    elapsed = time.time() - start
    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído em {elapsed:.0f}s")
    print(f"  {inserted} cidades importadas | {errors} erros")
    if args.only_meta:
        print("  Rode sem --only-meta para adicionar população e PIB")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
