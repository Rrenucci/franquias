"""
import_franchises.py
Importa o catálogo de franquias do Guia de Franquias Associadas ABF
(portaldofranchising.com.br) para a tabela `franchises` no Supabase.

É a fonte de dado real que faltava — sem ela, `franchises` fica vazia e
não tem o que recomendar nem o que cruzar com CNAE em company_lifecycle.

── Fonte e taxonomia de segmentos ────────────────────────────────────────
As 11 categorias abaixo são as usadas oficialmente pelo guia (extraídas
de https://www.portaldofranchising.com.br/guia-de-franquias-associadas-abf/,
não inventadas) — é essa a taxonomia que `franchises.segment` usa, e é a
mesma que scripts/import_cnpj_lifecycle.py deveria adotar no seu
CNAE_SEGMENT_MAP (hoje ele usa uma lista provisória diferente — alinhar
quando os dois forem rodados juntos).

── Estrutura da página (verificada em 2026-08-14 via curl direto) ────────
Cada card `.card-catafranchise` tem:
  - nome:        p.card-title
  - descrição:   p.card-text
  - investimento: p.investment-values → "80.000" ou "80.000 a 210.000"
  - link:        a.click-observe.logo[href]  (página de detalhe)
  - logo:        img[data-src]
Paginação via ?page=N — confirmado que devolve conteúdo diferente por
página (testado página 1 vs 2). Páginas de detalhe (~28MB cada, têm um
localizador de lojas embutido) NÃO são raspadas aqui por custo — os
campos que só existem lá (royalty_pct, payback, faturamento médio,
units_count) ficam NULL por enquanto. Ver TODO no fim do arquivo.

Uso:
  python scripts/import_franchises.py                    # todos os segmentos
  python scripts/import_franchises.py --segment alimentacao
  python scripts/import_franchises.py --dry-run           # só mostra o que acharia
"""

import os
import re
import sys
import time
import argparse
import unicodedata

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

BASE = "https://franquias.portaldofranchising.com.br"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FranchiseIntelligenceBot/1.0; +research)"}
REQUEST_PAUSE = 1.0   # segundos entre páginas, pra não martelar o site
MAX_PAGES_SAFETY = 30  # trava de segurança por segmento

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

# ─── Taxonomia oficial (slug da URL → segmento interno) ──────────────────────

SEGMENTS = {
    "alimentacao":                              "alimentacao",
    "casa-e-construcao":                        "casa_e_construcao",
    "servicos-educacionais":                    "educacao",
    "comunicacao-informatica-e-eletronicos":    "comunicacao_e_tecnologia",
    "entretenimento-e-lazer":                   "entretenimento_e_lazer",
    "hotelaria-e-turismo":                      "hotelaria_e_turismo",
    "limpeza-e-conservacao":                    "limpeza_e_conservacao",
    "moda":                                     "moda",
    "saude-beleza-e-bem-estar":                 "saude_beleza_bem_estar",
    "servicos-automotivos":                     "servicos_automotivos",
    "servicos-e-outros-negocios":               "servicos_e_outros_negocios",
}

# ─── Parsing ──────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return norm


def parse_investment(text: str) -> tuple[int | None, int | None]:
    text = text.strip().replace(".", "")
    if " a " in text:
        lo, hi = text.split(" a ", 1)
        try:
            return int(lo), int(hi)
        except ValueError:
            return None, None
    try:
        value = int(text)
        return value, value
    except ValueError:
        return None, None


def parse_card(card, segment: str) -> dict | None:
    title_el = card.select_one("p.card-title")
    if not title_el:
        return None
    name = title_el.get_text(strip=True)
    if not name:
        return None

    desc_el = card.select_one("p.card-text")
    description = desc_el.get_text(strip=True) if desc_el else None

    inv_el = card.select_one("p.investment-values")
    investment_min, investment_max = parse_investment(inv_el.get_text()) if inv_el else (None, None)

    link_el = card.select_one("a.click-observe.logo[href]")
    brand_url = link_el["href"] if link_el else None

    img_el = card.select_one("img[data-src]")
    logo_url = img_el["data-src"] if img_el else None

    return {
        "name": name,
        "slug": slugify(name),
        "segment": segment,
        "brand_url": brand_url,
        "logo_url": logo_url,
        "investment_min": investment_min,
        "investment_max": investment_max,
        "abf_member": True,
        "active": True,
        "data_source": "portal_do_franchising",
    }

# ─── Coleta por segmento (paginado) ──────────────────────────────────────────

def fetch_segment(url_slug: str, segment: str) -> list[dict]:
    header(f"Segmento: {segment} ({url_slug})")
    results: dict[str, dict] = {}  # dedup por slug

    for page in range(1, MAX_PAGES_SAFETY + 1):
        url = f"{BASE}/franquias-de-{url_slug}/?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"Falha na página {page}: {e}", "ERR")
            break

        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select("div.card-catafranchise")
        if not cards:
            log(f"Página {page}: sem resultados, parando paginação", "INFO")
            break

        new_count = 0
        for card in cards:
            franchise = parse_card(card, segment)
            if franchise and franchise["slug"] not in results:
                results[franchise["slug"]] = franchise
                new_count += 1

        log(f"Página {page}: {len(cards)} cards, {new_count} novos (total acumulado: {len(results)})")

        if new_count == 0:
            # página repetiu o que já tínhamos — provavelmente acabou a paginação real
            break

        time.sleep(REQUEST_PAUSE)

    log(f"{len(results)} franquias únicas em {segment}", "OK")
    return list(results.values())

# ─── Gravação no Supabase ────────────────────────────────────────────────────

def upsert_franchises(supabase, franchises: list[dict]) -> tuple[int, int]:
    header("Gravando no Supabase")
    inserted, errors = 0, 0
    BATCH = 50
    for i in range(0, len(franchises), BATCH):
        batch = franchises[i:i + BATCH]
        try:
            supabase.table("franchises").upsert(batch, on_conflict="slug").execute()
            inserted += len(batch)
        except Exception as e:
            errors += len(batch)
            log(f"Erro no lote {i}-{i+BATCH}: {e}", "ERR")
    log(f"{inserted} gravados | {errors} com erro", "OK" if errors == 0 else "WARN")
    return inserted, errors


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Importa catálogo de franquias do Guia ABF / Portal do Franchising")
    p.add_argument("--segment", type=str, default=None,
                   help=f"Só um segmento (slugs: {', '.join(SEGMENTS)})")
    p.add_argument("--dry-run", action="store_true", help="Não grava no Supabase, só mostra o que achou")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print(f"\n{BOLD}{'='*60}")
    print("  Franchise Intelligence — Importador de Franquias (ABF)")
    print(f"{'='*60}{RESET}\n")

    segments_to_run = SEGMENTS
    if args.segment:
        if args.segment not in SEGMENTS:
            log(f"Segmento desconhecido: {args.segment}", "ERR")
            log(f"Válidos: {', '.join(SEGMENTS)}", "INFO")
            sys.exit(1)
        segments_to_run = {args.segment: SEGMENTS[args.segment]}

    all_franchises: list[dict] = []
    for url_slug, segment in segments_to_run.items():
        all_franchises.extend(fetch_segment(url_slug, segment))
        time.sleep(REQUEST_PAUSE)

    header("Resumo da coleta")
    log(f"{len(all_franchises)} franquias no total", "OK")

    if args.dry_run:
        log("Modo --dry-run: nada foi gravado no Supabase", "WARN")
        for f in all_franchises[:10]:
            print(f"    {f['name']} | {f['segment']} | R$ {f['investment_min']}-{f['investment_max']}")
        if len(all_franchises) > 10:
            print(f"    ... e mais {len(all_franchises) - 10}")
        return

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    inserted, errors = upsert_franchises(supabase, all_franchises)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído — {inserted} franquias gravadas, {errors} erros")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()

# TODO: enriquecimento com dado de página de detalhe (royalty_pct,
# marketing_pct, payback_months_min/max, avg_monthly_revenue, units_count).
# Cada página de detalhe veio ~28MB no teste (tem um localizador de lojas
# embutido) — raspar todas as ~300-400 franquias assim é caro demais pra
# fazer sem critério. Se for fazer, vale rodar só pra um subconjunto
# (ex: as N franquias mais buscadas) e/ou achar o endpoint JSON que a
# página usa internamente pro localizador, que deve ser muito mais leve.
