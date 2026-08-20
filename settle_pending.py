#!/usr/bin/env python3
"""
Liquidación masiva de picks pendientes con TheSportsDB (gratis).
Consulta resultados por fecha (1 request por fecha) y liquida automáticamente.
Mercados de 1ª parte se dejan pendientes (sin dato fiable de HT).
"""
import os
import sys
import time
import logging
import unicodedata
import requests
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta

from stats_tracker import StatsTracker

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

BASE = "https://www.thesportsdb.com/api/v1/json/123"


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")


def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ''.join(c for c in s.lower() if c.isalnum() or c == ' ').strip()


def sim(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def get_events_day(date_str, cache):
    """Devuelve los eventos de fútbol de una fecha (con caché por fecha)."""
    if date_str in cache:
        return cache[date_str]
    events = []
    try:
        r = requests.get(f"{BASE}/eventsday.php",
                         params={"d": date_str, "s": "Soccer"}, timeout=15)
        if r.status_code == 200:
            events = r.json().get("events") or []
            logger.info(f"📅 {date_str}: {len(events)} eventos de fútbol")
        else:
            logger.error(f"❌ TheSportsDB {date_str}: HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"❌ TheSportsDB {date_str}: {e}")
    cache[date_str] = events
    time.sleep(1.2)  # rate limit del tier gratuito
    return events


def find_match(events, home, away):
    """Busca el evento que mejor casa con los equipos del pick."""
    nh, na = norm(home), norm(away)
    best, best_score = None, 0.0
    for ev in events:
        eh = norm(ev.get("strHomeTeam") or "")
        ea = norm(ev.get("strAwayTeam") or "")
        score = sim(nh, eh) + sim(na, ea)
        if score > best_score:
            best_score, best = score, ev
    return best if best_score >= 1.6 else None


def parse_pick_date(hora):
    """Convierte 'DD/MM HH:MM' a fecha ISO, gestionando el cruce de año."""
    try:
        date_part = hora.split()[0]
        day, month = date_part.split('/')
        today = datetime.now(timezone.utc).date()
        year = today.year
        d = datetime(year, int(month), int(day)).date()
        if d > today + timedelta(days=1):
            d = datetime(year - 1, int(month), int(day)).date()
        return d.isoformat(), d
    except Exception:
        return None, None


def evaluate_pick(pick, hg, ag):
    """Evalúa won/lost con el resultado (hg=goles local, ag=goles visitante)."""
    mercado = (pick.get('mercado') or '').lower()
    total = hg + ag

    # Mercados de 1ª parte: sin dato fiable → no liquidar
    if '1ª parte' in mercado or ' ht' in mercado:
        return None

    if 'over 0.5' in mercado:
        return 'won' if total >= 1 else 'lost'
    if 'over 1.5' in mercado:
        return 'won' if total >= 2 else 'lost'
    if 'over 2.5' in mercado:
        return 'won' if total >= 3 else 'lost'
    if 'over 3.5' in mercado:
        return 'won' if total >= 4 else 'lost'
    if 'under 0.5' in mercado:
        return 'won' if total == 0 else 'lost'
    if 'under 1.5' in mercado:
        return 'won' if total <= 1 else 'lost'
    if 'under 2.5' in mercado:
        return 'won' if total <= 2 else 'lost'
    if 'under 3.5' in mercado:
        return 'won' if total <= 3 else 'lost'

    if 'btts' in mercado:
        both = hg >= 1 and ag >= 1
        if 'no' in mercado:
            return 'won' if not both else 'lost'
        return 'won' if both else 'lost'

    if mercado.startswith('1x2'):
        parte = mercado.split('-', 1)[1].strip() if '-' in mercado else ''
        partido = pick.get('partido', '')
        home = partido.split(' vs ')[0] if ' vs ' in partido else ''
        away = partido.split(' vs ')[1] if ' vs ' in partido else ''
        if 'empate' in parte or 'draw' in parte:
            return 'won' if hg == ag else 'lost'
        if sim(norm(parte), norm(home)) >= 0.8:
            return 'won' if hg > ag else 'lost'
        if sim(norm(parte), norm(away)) >= 0.8:
            return 'won' if ag > hg else 'lost'
        return None

    if 'doble oportunidad' in mercado or mercado.startswith('dc'):
        if hg > ag:
            return 'won' if ('1x' in mercado or '12' in mercado) else 'lost'
        if ag > hg:
            return 'won' if ('x2' in mercado or '12' in mercado) else 'lost'
        return 'won' if ('1x' in mercado or 'x2' in mercado) else 'lost'

    return None


def main():
    logger.info("🚀 Iniciando liquidación masiva (TheSportsDB)...")

    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1

    pending = [p for p in tracker.get_all_picks() if p.get('status') == 'pending']
    if not pending:
        logger.info("✅ No hay picks pendientes")
        return 0

    logger.info(f"📋 {len(pending)} picks pendientes")

    cache = {}
    settled = won = lost = void = skipped = not_found = 0

    for pick in pending:
        partido = pick.get('partido', '')
        if ' vs ' not in partido:
            skipped += 1
            continue
        home, away = partido.split(' vs ', 1)

        date_str, d = parse_pick_date(pick.get('hora', ''))
        if not date_str:
            skipped += 1
            continue

        # Partidos de hoy o futuros: aún no jugados
        if d >= datetime.now(timezone.utc).date():
            not_found += 1
            continue

        events = get_events_day(date_str, cache)
        ev = find_match(events, home, away)
        if not ev:
            not_found += 1
            logger.info(f"⏳ {partido} ({date_str}): sin coincidencia en TheSportsDB")
            continue

        hg = ev.get('intHomeScore')
        ag = ev.get('intAwayScore')
        if hg is None or ag is None or str(hg).strip() == '' or str(ag).strip() == '':
            not_found += 1
            continue
        hg, ag = int(hg), int(ag)

        status = evaluate_pick(pick, hg, ag)
        if status is None:
            skipped += 1
            logger.info(f"⏭️ {partido}: mercado sin liquidar (HT o no reconocido)")
            continue

        tracker.settle_pick(pick['id'], status)
        settled += 1
        if status == 'won':
            won += 1
            logger.info(f"✅ {partido}: WON ({hg}-{ag})")
        elif status == 'lost':
            lost += 1
            logger.info(f"❌ {partido}: LOST ({hg}-{ag})")
        else:
            void += 1
            logger.info(f"➖ {partido}: VOID")

    summary = (f"🤖 <b>LIQUIDACIÓN MASIVA COMPLETADA</b>\n\n"
               f"📊 Liquidados: {settled} (✅ {won} · ❌ {lost} · ➖ {void})\n"
               f"⏳ Sin resultado aún: {not_found}\n"
               f"⏭️ Omitidos (HT/no reconocidos): {skipped}\n\n"
               f"💡 Usa /stats para ver el rendimiento actualizado.")
    send_telegram(summary)
    logger.info(f"✅ Liquidación completada: {settled} procesados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
