"""
enrich_investment_models.py
Segunda passada de enriquecimento — descobri, checando manualmente a resposta
do chat sobre Itapecerica, que a maioria das franquias sem `faq_data` (o bloco
JSON-LD FAQPage) na verdade TEM dados de investimento publicados, só que numa
estrutura HTML diferente: a tabela oficial de "Investimento por modelo" que a
própria ABF exibe por aba (Loja, Home Based, Quiosque etc.), com granularidade
maior que o FAQ — payback, royalties (tipo + valor + base de cálculo), taxa de
propaganda, faturamento médio mensal, número de unidades, sede.

Isso não é chute: é o mesmo texto declarado pela franqueadora à ABF, só que
fora do schema.org FAQPage. Guarda tudo em `investment_models` (JSONB, uma
lista — uma entry por modelo de operação) e usa o conjunto pra preencher os
campos agregados já existentes (royalty_pct, payback_months_min/max,
units_count) só quando não há ambiguidade.

Uso:
  python scripts/enrich_investment_models.py                # todas sem investment_models ainda
  python scripts/enrich_investment_models.py --force         # re-busca todas
  python scripts/enrich_investment_models.py --slug tz-viagens
"""

import os
import re
import sys
import time
import argparse

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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FranchiseIntelligenceBot/1.0; +research)"}
READ_LIMIT = 450_000  # bytes — a tabela de investimento apareceu bem antes disso em todas as amostras testadas
REQUEST_PAUSE = 1.0

CYAN, GREEN, YELLOW, RED, RESET, BOLD = (
    "\033[96m", "\033[92m", "\033[93m", "\033[91m", "\033[0m", "\033[1m"
)


def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": f"{CYAN}•{RESET}", "OK": f"{GREEN}✓{RESET}",
              "WARN": f"{YELLOW}⚠{RESET}", "ERR": f"{RED}✗{RESET}"}
    print(f"  {icons.get(level, '•')} {msg}")


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (50 - len(title))}{RESET}")

# ─── Fetch parcial (early-abort) ──────────────────────────────────────────

def fetch_partial_html(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = requests.get(url, headers=HEADERS, stream=True, timeout=30)
    r.raise_for_status()
    buf = b""
    for chunk in r.iter_content(chunk_size=8192):
        buf += chunk
        if len(buf) >= READ_LIMIT:
            break
    r.close()
    return buf.decode("utf-8", errors="replace")

# ─── Parsing de números em formato BR ─────────────────────────────────────

def parse_brl_number(token: str) -> float | None:
    token = token.strip()
    if not token:
        return None
    token = re.sub(r"[Rr]\$", "", token).strip()
    if "," in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(".", "")
    token = re.sub(r"[^\d.]", "", token)
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_number_or_range(text: str) -> tuple[float | None, float | None]:
    """'20.000 a 40.000' -> (20000, 40000); '9.500' -> (9500, 9500)"""
    tokens = re.findall(r"[\d.,]+", text)
    values = [parse_brl_number(t) for t in tokens]
    values = [v for v in values if v is not None]
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    return min(values), max(values)


def parse_payback_months(text: str) -> tuple[int | None, int | None]:
    text = text.lower()
    m = re.search(r"(\d+)\s*a\s*(\d+)\s*(m[eê]s|mes)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*(m[eê]s|mes)", text)
    if m:
        v = int(m.group(1))
        return v, v
    m = re.search(r"(\d+)\s*a\s*(\d+)\s*ano", text)
    if m:
        return int(m.group(1)) * 12, int(m.group(2)) * 12
    m = re.search(r"(\d+)\s*ano", text)
    if m:
        v = int(m.group(1)) * 12
        return v, v
    return None, None


def classify_fee(taxa_text: str | None) -> dict:
    """Retorna {type, value, is_percent} pra um campo 'Taxa' de royalties/propaganda."""
    if not taxa_text:
        return {"type": None, "value": None}
    t = taxa_text.strip().lower()
    if "não cobra" in t or "nao cobra" in t or "isento" in t:
        return {"type": "none", "value": 0}
    if "%" in taxa_text:
        m = re.search(r"([\d.,]+)\s*%", taxa_text)
        return {"type": "percent", "value": parse_brl_number(m.group(1)) if m else None}
    if "r$" in t or re.search(r"\d", taxa_text):
        return {"type": "fixed_brl", "value": parse_brl_number(taxa_text)}
    return {"type": None, "value": None}

# ─── Extração da tabela de investimento por modelo ────────────────────────

def extract_investment_models(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    models = []

    for tab in soup.select("div.tab-content"):
        classes = tab.get("class", [])
        model_name = next((c for c in classes if c not in ("tab-content", "d-none")), None)
        if not model_name or model_name == "about":
            continue

        table = tab.select_one("table.table")
        if not table:
            continue

        entry = {"model": model_name}

        for row in table.select("tr"):
            cells = row.find_all("th")
            if len(cells) != 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            value_text = cells[1].get_text(" ", strip=True)
            lo, hi = parse_number_or_range(value_text)
            if "instala" in label:
                entry["installation_capital_min"], entry["installation_capital_max"] = lo, hi
            elif "franquia" in label:
                entry["franchise_fee"] = lo
            elif "capital de giro" in label:
                entry["working_capital_min"], entry["working_capital_max"] = lo, hi
            elif "investimento total" in label:
                entry["total_investment_min"], entry["total_investment_max"] = lo, hi

        # Blocos "column-icon": sequência de title/content, com "Taxa de
        # propaganda" e "ROYALTIES" funcionando como cabeçalho de sub-seção
        # (title sem content) seguido de "Taxa" + "Base de cálculo".
        section = None
        pending_fee = {}
        for icon in tab.select(".column-icon"):
            title_el = icon.select_one(".title")
            content_el = icon.select_one(".content")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            content = content_el.get_text(strip=True) if content_el else None

            if title in ("Taxa de propaganda", "ROYALTIES") and content is None:
                if section and pending_fee:
                    entry[f"{section}_fee"] = pending_fee
                section = "marketing" if title == "Taxa de propaganda" else "royalty"
                pending_fee = {}
                continue

            if title == "Taxa" and section:
                pending_fee.update(classify_fee(content))
            elif title == "Base de cálculo" and section:
                pending_fee["base"] = content
            elif title == "Retorno do investimento":
                lo, hi = parse_payback_months(content or "")
                entry["payback_months_min"], entry["payback_months_max"] = lo, hi
            elif title == "Faturamento médio mensal":
                entry["avg_monthly_revenue"] = parse_brl_number(content or "")
            elif title == "Sede":
                entry["hq_state"] = content
            elif "unidades" in title.lower():
                m = re.search(r"\d+", content or "")
                entry["units_count"] = int(m.group()) if m else None
            elif "área" in title.lower() or "area" in title.lower():
                entry["store_area"] = content

        if section and pending_fee:
            entry[f"{section}_fee"] = pending_fee

        if len(entry) > 1:  # tem mais que só "model"
            models.append(entry)

    return models


def aggregate_scalars(models: list[dict]) -> dict:
    paybacks_min = [m["payback_months_min"] for m in models if m.get("payback_months_min") is not None]
    paybacks_max = [m["payback_months_max"] for m in models if m.get("payback_months_max") is not None]
    units = [m["units_count"] for m in models if m.get("units_count") is not None]
    royalty_pcts = [m["royalty_fee"]["value"] for m in models
                    if m.get("royalty_fee", {}).get("type") == "percent" and m["royalty_fee"].get("value") is not None]

    return {
        "payback_months_min": min(paybacks_min) if paybacks_min else None,
        "payback_months_max": max(paybacks_max) if paybacks_max else None,
        "units_count": max(units) if units else None,
        "royalty_pct": royalty_pcts[0] if royalty_pcts else None,
    }

# ─── Pipeline ──────────────────────────────────────────────────────────────

def enrich_franchise(supabase, franchise: dict) -> bool:
    url = franchise.get("brand_url")
    if not url:
        log(f"{franchise['name']}: sem brand_url, pulando", "WARN")
        return False

    try:
        html = fetch_partial_html(url)
    except requests.RequestException as e:
        log(f"{franchise['name']}: falha ao buscar ({e})", "ERR")
        return False

    models = extract_investment_models(html)
    if not models:
        log(f"{franchise['name']}: nenhuma tabela de investimento encontrada", "WARN")
        return False

    scalars = aggregate_scalars(models)
    payload = {"investment_models": models, "investment_models_fetched_at": "now()"}
    payload.update({k: v for k, v in scalars.items() if v is not None})

    supabase.table("franchises").update(payload).eq("id", franchise["id"]).execute()
    log(f"{franchise['name']}: {len(models)} modelo(s) — payback={scalars['payback_months_min']}-{scalars['payback_months_max']}m "
        f"royalty%={scalars['royalty_pct']} unidades={scalars['units_count']}", "OK")
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enriquece franquias com a tabela oficial de investimento por modelo")
    p.add_argument("--force", action="store_true", help="Re-busca mesmo franquias que já têm investment_models")
    p.add_argument("--slug", type=str, default=None, help="Só uma franquia, pra testar")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY}.items() if not v]
    if missing:
        log(f"Variáveis ausentes no .env: {', '.join(missing)}", "ERR")
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    header("Buscando franquias")
    query = supabase.table("franchises").select("id, name, slug, brand_url, investment_models")
    if args.slug:
        query = query.eq("slug", args.slug)
    franchises = query.execute().data

    if not args.force and not args.slug:
        franchises = [f for f in franchises if not f.get("investment_models")]

    log(f"{len(franchises)} franquias pra processar", "INFO")

    header("Enriquecendo (early-abort ~450KB por página)")
    ok, fail = 0, 0
    for i, f in enumerate(franchises, 1):
        print(f"  [{i}/{len(franchises)}]", end=" ")
        if enrich_franchise(supabase, f):
            ok += 1
        else:
            fail += 1
        time.sleep(REQUEST_PAUSE)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído — {ok} enriquecidas, {fail} sem tabela de investimento")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
