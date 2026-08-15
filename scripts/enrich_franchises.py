"""
enrich_franchises.py
Enriquece o catálogo de franquias com payback, royalties, lucratividade e
número de unidades — dado que faltava desde o import original.

── Por que isso não veio no import_franchises.py ─────────────────────────
A página de detalhe de cada franquia tem ~28MB (tem um localizador de lojas
embutido num único <script>). Baixar as 187 páginas inteiras seria ~5GB.

── Como resolvido ─────────────────────────────────────────────────────────
O dado que interessa (payback, taxas, lucratividade) está nos primeiros
~35KB do HTML, num bloco JSON-LD `FAQPage` padronizado — a mesma pergunta 1
é sempre "Qual o investimento total?", a 2 é "Quais são as taxas mensais?",
a 3 é "Qual o prazo de retorno (Payback)?", a 4 é "Qual a lucratividade?".
Isso é dado estruturado pensado pra SEO, não um chute de regex numa página
qualquer. Testado: parar de ler o stream em ~200KB pega o bloco inteiro,
1.6s por página em vez do minuto+ que levaria a página completa.

Guarda o FAQ inteiro (10 perguntas/respostas) em `faq_data` (JSONB) — é a
fonte confiável, em linguagem natural, pro futuro recurso de chat/explicação.
Além disso tenta extrair best-effort pros campos numéricos já existentes no
schema (royalty_pct, payback_months_min/max, units_count) — quando o regex
não bate com confiança, fica NULL em vez de arriscar um número errado.

Uso:
  python scripts/enrich_franchises.py                 # todas as franquias sem faq_data ainda
  python scripts/enrich_franchises.py --force          # re-busca mesmo as que já têm
  python scripts/enrich_franchises.py --slug puket     # só uma, pra testar
"""

import os
import re
import sys
import json
import time
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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FranchiseIntelligenceBot/1.0; +research)"}
READ_LIMIT = 260_000  # bytes — suficiente pra pegar o FAQPage inteiro (~35KB) com folga
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
    r = requests.get(url, headers=HEADERS, stream=True, timeout=30)
    r.raise_for_status()
    buf = b""
    for chunk in r.iter_content(chunk_size=8192):
        buf += chunk
        if len(buf) >= READ_LIMIT:
            break
    r.close()
    return buf.decode("utf-8", errors="replace")

# ─── Extração ──────────────────────────────────────────────────────────────

def extract_faq(html: str) -> list[dict] | None:
    idx = html.find('"@type":"FAQPage"')
    if idx == -1:
        return None
    # acha o <script> que contém esse trecho e isola o JSON
    script_start = html.rfind("<script", 0, idx)
    script_content_start = html.find(">", script_start) + 1
    script_end = html.find("</script>", script_content_start)
    if script_end == -1:
        return None
    raw = html[script_content_start:script_end]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    faq = []
    for item in data.get("mainEntity", []):
        question = item.get("name", "")
        answer = item.get("acceptedAnswer", {}).get("text", "")
        if question and answer:
            faq.append({"question": question, "answer": answer})
    return faq or None


def extract_units_count(html: str) -> int | None:
    idx = html.find("unidades tem no Brasil")
    if idx == -1:
        return None
    window = html[idx:idx + 500]
    m = re.search(r">\s*(\d[\d.]*)\s*<", window)
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", ""))
    except ValueError:
        return None


def parse_payback_months(answer: str) -> tuple[int | None, int | None]:
    m = re.search(r"(\d+)\s*a\s*(\d+)\s*mes", answer, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*mes", answer, re.IGNORECASE)
    if m:
        v = int(m.group(1))
        return v, v
    return None, None


def parse_royalty_pct(answer: str) -> float | None:
    lowered = answer.lower()
    if "não cobra royalties" in lowered or "sem royalties" in lowered or "não há royalties" in lowered:
        return 0.0
    m = re.search(r"royalties?[^%]{0,40}?(\d+(?:[.,]\d+)?)\s*%", answer, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def enrich_from_faq(faq: list[dict]) -> dict:
    result = {"royalty_pct": None, "payback_months_min": None, "payback_months_max": None}
    for item in faq:
        q = item["question"].lower()
        a = item["answer"]
        if "taxas mensais" in q or "royalties" in q:
            result["royalty_pct"] = parse_royalty_pct(a)
        elif "payback" in q or "prazo de retorno" in q:
            lo, hi = parse_payback_months(a)
            result["payback_months_min"], result["payback_months_max"] = lo, hi
    return result

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

    faq = extract_faq(html)
    if not faq:
        log(f"{franchise['name']}: FAQPage não encontrado nos primeiros {READ_LIMIT//1000}KB", "WARN")
        return False

    numeric = enrich_from_faq(faq)
    units_count = extract_units_count(html)

    payload = {"faq_data": faq, "faq_fetched_at": "now()"}
    payload.update({k: v for k, v in numeric.items() if v is not None})
    if units_count is not None:
        payload["units_count"] = units_count

    supabase.table("franchises").update(payload).eq("id", franchise["id"]).execute()
    log(f"{franchise['name']}: payback={numeric['payback_months_min']}-{numeric['payback_months_max']}m "
        f"royalty={numeric['royalty_pct']}% unidades={units_count}", "OK")
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enriquece franquias com FAQ estruturado (payback, royalties, unidades)")
    p.add_argument("--force", action="store_true", help="Re-busca mesmo franquias que já têm faq_data")
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
    query = supabase.table("franchises").select("id, name, slug, brand_url, faq_data")
    if args.slug:
        query = query.eq("slug", args.slug)
    franchises = query.execute().data

    if not args.force and not args.slug:
        franchises = [f for f in franchises if not f.get("faq_data")]

    log(f"{len(franchises)} franquias pra processar", "INFO")

    header("Enriquecendo (early-abort ~200KB por página)")
    ok, fail = 0, 0
    for i, f in enumerate(franchises, 1):
        print(f"  [{i}/{len(franchises)}]", end=" ")
        if enrich_franchise(supabase, f):
            ok += 1
        else:
            fail += 1
        time.sleep(REQUEST_PAUSE)

    print(f"\n{BOLD}{GREEN}{'='*60}")
    print(f"  Concluído — {ok} enriquecidas, {fail} falharam")
    print(f"{'='*60}{RESET}\n")


if __name__ == "__main__":
    main()
