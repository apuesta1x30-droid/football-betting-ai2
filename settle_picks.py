#!/usr/bin/env python3
"""
v0.2 · Liquidación automática de picks con resultados REALES de API-Football.
Lee picks 'pending' de Supabase, consulta el resultado final (FT/HT)
y los liquida como won / lost / void. Opcionalmente envía un resumen
por Telegram con el resultado del día y el acumulado.

Uso manual (solo interfaz web de GitHub; el workflow lo ejecuta solo):
  python settle_picks.py              # liquida tabla 'picks'
  python settle_picks.py --table X    # otra tabla (tests)
"""
import os
import re
import sys
import logging
import argparse
import requests
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY', '')
API_HEADERS = {
    'x-rapidapi-key': API_FOOTBALL_KEY,
    'x-rapidapi-host': 'v3.football.api-sports.io'
}
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

FINISHED = {'FT', 'AET', 'PEN'}          # resultado definitivo
VOID_STATUSES = {'CANC', 'ABD', 'AWD', 'WO'}  # anulados
MAX_AGE_DAYS = 10                          # ventana de liquidación


# ==========================================
# UTILIDADES
# ==========================================
def _norm(s):
    """Normaliza texto: minúsculas, sin tildes ni diacríticos."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def send_telegram_message(message, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": parse_mode, "disable_web_page_preview": True
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"❌ Error Telegram: {e}")
        return False


def pick_match_dt(p):
    """Fecha/hora del partido (Madrid) a partir de 'hora' + año del timestamp."""
    try:
        year = int(p['timestamp'][:4])
        return datetime.strptime(f"{p['hora']} {year}", '%d/%m %H:%M %Y').replace(tzinfo=ZoneInfo("Europe/Madrid"))
    except Exception:
        return None


def candidate_dates(dt):
    """Fechas UTC posibles del fixture (Madrid va por delante de UTC)."""
    return [dt.date().isoformat(), (dt - timedelta(days=1)).date().isoformat()]


def fetch_fixtures(cache, date_str):
    if date_str in cache:
        return cache[date_str]
    try:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers=API_HEADERS,
            params={"date": date_str, "timezone": "UTC"},
            timeout=15
        )
        data = r.json().get("response", [])
    except Exception as e:
        logger.error(f"Error consultando fixtures {date_str}: {e}")
        data = []
    cache[date_str] = data
    return data


def find_fixture(fixtures, home, away):
    h, a = _norm(home), _norm(away)
    for f in fixtures:
        t = f.get('teams', {})
        hn = _norm((t.get('home') or {}).get('name', ''))
        an = _norm((t.get('away') or {}).get('name', ''))
        if (h in hn or hn in h) and (a in an or an in a):
            return f
    return None


def get_scores(f):
    """Devuelve (fh, fa, hh, ha): goles FT y HT. Prioriza score.fulltime (90')."""
    score = f.get('score') or {}
    ft = score.get('fulltime') or {}
    ht = score.get('halftime') or {}
    goals = f.get('goals') or {}
    fh = ft.get('home', goals.get('home'))
    fa = ft.get('away', goals.get('away'))
    hh = ht.get('home')
    ha = ht.get('away')
    return fh, fa, hh, ha


# ==========================================
# LIQUIDACIÓN POR MERCADO
# ==========================================
def settle_market(mercado, home, away, fh, fa, hh, ha):
    m = _norm(mercado)

    # Over 0.5 1ª Parte
    if '1a parte' in m or 'primera parte' in m:
        if hh is None or ha is None:
            return None
        mo = re.search(r'over\s+(\d+(?:\.\d+)?)', m)
        if mo:
            return 'won' if (hh + ha) > float(mo.group(1)) else 'lost'
        return None

    # Over X Goles (FT)
    mo = re.match(r'over\s+(\d+(?:\.\d+)?)\s+goles', m)
    if mo:
        if fh is None or fa is None:
            return None
        return 'won' if (fh + fa) > float(mo.group(1)) else 'lost'

    # BTTS
    if m.startswith('btts'):
        if fh is None or fa is None:
            return None
        sel = m.split('-', 1)[1].strip()
        both = (fh > 0 and fa > 0)
        if sel.startswith('si'):
            return 'won' if both else 'lost'
        if sel.startswith('no'):
            return 'won' if not both else 'lost'
        return None

    # 1X2
    if m.startswith('1x2'):
        if fh is None or fa is None:
            return None
        sel = m.split('-', 1)[1].strip()
        if sel in ('empate', 'draw'):
            return 'won' if fh == fa else 'lost'
        if sel == _norm(home):
            return 'won' if fh > fa else 'lost'
        if sel == _norm(away):
            return 'won' if fa > fh else 'lost'
        return None

    # Doble Oportunidad
    if m.startswith('doble oportunidad'):
        if fh is None or fa is None:
            return None
        code = m.split('-', 1)[1].strip()
        if code == '1x':
            return 'won' if fh >= fa else 'lost'
        if code == 'x2':
            return 'won' if fa >= fh else 'lost'
        if code == '12':
            return 'won' if fh != fa else 'lost'
        return None

    return None  # mercado no reconocido → queda pending (liquidación manual)


# ==========================================
# PROCESO PRINCIPAL
# ==========================================
def run_settlement(table):
    tracker = StatsTracker(table_name=table)
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1
    if not API_FOOTBALL_KEY:
        logger.error("❌ API_FOOTBALL_KEY no configurada")
        return 1

    now_utc = datetime.now(timezone.utc)
    now_madrid = now_utc.astimezone(ZoneInfo("Europe/Madrid"))

    pending = [p for p in tracker.get_all_picks() if p['status'] == 'pending']
    logger.info(f"🔎 {len(pending)} picks pendientes")

    # Filtrar ventana temporal
    candidates = []
    for p in pending:
        dt = pick_match_dt(p)
        if not dt:
            continue
        if dt > now_madrid:
            continue  # el partido aún no se ha jugado
        if dt < now_madrid - timedelta(days=MAX_AGE_DAYS):
            continue  # demasiado antiguo → liquidación manual
        candidates.append((p, dt))

    cache = {}
    results = []  # (pick, status, marcador)

    for p, dt in candidates:
        home, _, away = (p['partido'] or '').partition(' vs ')
        fixture = None
        for date_str in candidate_dates(dt):
            fixture = find_fixture(fetch_fixtures(cache, date_str), home, away)
            if fixture:
                break
        if not fixture:
            logger.warning(f"⚠️ Fixture no encontrado: {p['partido']}")
            continue

        status_short = ((fixture.get('fixture') or {}).get('status') or {}).get('short', '')
        if status_short in VOID_STATUSES:
            tracker.settle_pick(p['id'], 'void')
            results.append((p, 'void', status_short))
            continue
        if status_short not in FINISHED:
            continue  # aún no terminado

        fh, fa, hh, ha = get_scores(fixture)
        outcome = settle_market(p['mercado'], home, away, fh, fa, hh, ha)
        if outcome is None:
            logger.warning(f"⚠️ No liquidable: {p['partido']} | {p['mercado']}")
            continue

        tracker.settle_pick(p['id'], outcome)
        results.append((p, outcome, f"{fh}-{fa} (HT {hh}-{ha})"))
        logger.info(f"{'✅' if outcome == 'won' else '❌'} #{p['id']} {p['partido']} | {p['mercado']} → {outcome}")

    # ==========================================
    # RESUMEN TELEGRAM
    # ==========================================
    if results:
        lines = []
        for p, outcome, score in results[:20]:
            emoji = {'won': '✅', 'lost': '❌', 'void': '➖'}[outcome]
            lines.append(f"{emoji} {p['partido']}\n   {p['mercado']} @ {p['cuota']:.2f} → {score}")
        extra = len(results) - 20
        if extra > 0:
            lines.append(f"… y {extra} más")

        s = tracker.calculate_stats()
        day_pnl = sum(
            (p['cuota'] - 1) if o == 'won' else (0 if o == 'void' else -1)
            for p, o, _ in results if p['cuota']
        )
        msg = (
            f"🧾 <b>LIQUIDACIÓN AUTOMÁTICA</b>\n\n"
            + "\n\n".join(lines)
            + f"\n\n📅 PnL del día: <b>{day_pnl:+.2f} u</b>"
            + f"\n💰 Acumulado: <b>{s['pnl']:+.2f} u</b> | Yield <b>{s['yield']:+.1f}%</b> "
            + f"({s['wins']}W-{s['losses']}L)"
        )
        if send_telegram_message(msg):
            logger.info("📨 Resumen de liquidación enviado a Telegram")

    logger.info(f"🏁 Liquidación completada: {len(results)} picks procesados")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="picks")
    args = ap.parse_args()
    sys.exit(run_settlement(args.table))
