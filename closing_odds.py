#!/usr/bin/env python3
"""
v0.5 · Captura de cuota de cierre para CLV.
Cambios vs v0.4-D:
  - Ventana ampliada a [-1h, +10h] respecto al kickoff: desde 10h antes
    hasta 1h después (por si el kickoff ya empezó y la cuota se fijó).
  - Sobrescribe closing_odds en cada captura: el último run antes del
    kickoff gana y captura el precio más cercano al cierre real.
  - Excluye del CLV (marca como 'late_bet') picks registrados a menos
    de 2h del kickoff: su CLV es 0% por construcción, no por edge.
  - Añadidos crons complementarios en closing_odds.yml.
Usa odds_client.py para rotación de claves y contador mensual.
"""
import os
import re
import sys
import json
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from stats_tracker import StatsTracker
from settle_picks import pick_match_dt, _norm
from odds_client import odds_get, get_keys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Ventana de captura: desde 10h ANTES del kickoff hasta 1h DESPUÉS.
# Así, con 5 crons/día, todo pick tiene múltiples oportunidades de captura.
WINDOW_BEFORE_HOURS = 10.0
WINDOW_AFTER_HOURS  = 1.0

# Picks registrados a menos de 2h del kickoff: su CLV es 0% por construcción
# (el mercado apenas tuvo tiempo de moverse). Se marcan y excluyen del CLV.
LATE_BET_HOURS = 2.0

META_SPORTS_KEY = 'odds_api_sports_map'


def get_sports_map(tracker):
    """Mapa título de liga → sport_key de The Odds API (cacheado 7 días en meta)."""
    try:
        resp = tracker.client.table('meta').select('value').eq('key', META_SPORTS_KEY).execute()
        if resp.data:
            cached = json.loads(resp.data[0]['value'])
            ts = datetime.fromisoformat(cached['ts'])
            if (datetime.now(timezone.utc) - ts).days < 7:
                return cached['map']
    except Exception as e:
        logger.debug(f"Caché de deportes no disponible: {e}")
    r = odds_get("sports/", tracker=tracker)
    if r is None or r.status_code != 200:
        logger.error("Error listando deportes (todas las claves agotadas)")
        return {}
    mapping = {s['title']: s['key'] for s in r.json() if s.get('active')}
    tracker.client.table('meta').upsert(
        {'key': META_SPORTS_KEY,
         'value': json.dumps({'ts': datetime.now(timezone.utc).isoformat(), 'map': mapping})},
        on_conflict='key').execute()
    return mapping


def market_target(mercado, home, away):
    """Devuelve (market_key, outcome_name, point) o None si el mercado no tiene cierre."""
    m = _norm(mercado)
    if '1a parte' in m or 'primera parte' in m:
        return None
    mo = re.match(r'over\s+(\d+(?:\.\d+)?)\s+goles', m)
    if mo:
        return ('totals', 'Over', float(mo.group(1)))
    if m.startswith('1x2'):
        sel = m.split('-', 1)[1].strip()
        if sel in ('empate', 'draw'):
            return ('h2h', 'Draw', None)
        if sel == _norm(home):
            return ('h2h', home, None)
        if sel == _norm(away):
            return ('h2h', away, None)
    return None


def extract_closing(event, mk, out_name, point):
    """Mejor cuota actual del mercado/outcome objetivo entre todas las casas."""
    best = None
    for bm in event.get('bookmakers', []):
        for market in bm.get('markets', []):
            if market.get('key') != mk:
                continue
            for out in market.get('outcomes', []):
                if point is not None:
                    if out.get('name') == out_name and abs((out.get('point') or -1) - point) < 1e-9:
                        best = max(best or 0, out['price'])
                else:
                    if _norm(out.get('name', '')) == _norm(out_name):
                        best = max(best or 0, out['price'])
    return best


def run(test=False):
    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1
    if not get_keys():
        logger.error("❌ No hay claves de The Odds API configuradas")
        return 1

    now = datetime.now(timezone.utc)
    now_madrid = now.astimezone(ZoneInfo("Europe/Madrid"))

    cand = []
    late_bets = 0
    for p in tracker.get_all_picks():
        if p['status'] != 'pending':
            continue
        dt = pick_match_dt(p)
        if not dt:
            continue
        # Marcar late_bets en el primer pase (solo una vez por pick)
        hours_to_kickoff = (dt - now_madrid).total_seconds() / 3600
        if 0 < hours_to_kickoff < LATE_BET_HOURS and not p.get('late_bet'):
            if not test:
                (tracker.client.table(tracker.table)
                 .update({'late_bet': True}).eq('id', p['id']).execute())
            late_bets += 1
            continue
        # Ventana ampliada: [-1h, +10h] respecto al kickoff
        if -WINDOW_AFTER_HOURS <= hours_to_kickoff <= WINDOW_BEFORE_HOURS:
            cand.append(p)

    logger.info(f"🔎 Picks en ventana de cierre: {len(cand)} · "
                f"late_bets marcados este run: {late_bets}")
    if not cand:
        return 0

    sports_map = get_sports_map(tracker)

    by_league = {}
    for p in cand:
        by_league.setdefault(p['liga'], []).append(p)

    captured = 0
    for liga, plist in by_league.items():
        sport_key = sports_map.get(liga)
        if not sport_key:
            logger.warning(f"⚠️ Liga sin sport_key activo: {liga}")
            continue
        r = odds_get(f"sports/{sport_key}/odds", {
            "regions": "eu,us",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
        }, tracker=tracker)
        if r is None or r.status_code != 200:
            logger.error(f"❌ The Odds API ({liga}): claves agotadas o tope mensual")
            continue
        events = r.json()
        if not isinstance(events, list):
            logger.error(f"❌ Respuesta inesperada ({liga}): {str(events)[:200]}")
            continue

        for p in plist:
            home, _, away = (p['partido'] or '').partition(' vs ')
            target = market_target(p['mercado'], home, away)
            if not target:
                continue
            ev = next((e for e in events
                       if _norm(e.get('home_team', '')) == _norm(home)
                       and _norm(e.get('away_team', '')) == _norm(away)), None)
            if not ev:
                logger.warning(f"⚠️ Evento no encontrado: {p['partido']}")
                continue
            mk, out_name, point = target
            closing = extract_closing(ev, mk, out_name, point)
            if not closing:
                logger.warning(f"⚠️ Sin cuota de cierre: {p['partido']} | {p['mercado']}")
                continue

            clv = ((p['cuota'] / closing) - 1) * 100 if p['cuota'] else 0.0

            if test:
                logger.info(f"🧪 {p['partido']} | {p['mercado']} | "
                            f"tomada {p['cuota']:.2f} | cierre {closing:.2f} | CLV {clv:+.1f}%")
                continue

            # Sobrescritura: cada run actualiza con la cuota más reciente
            (tracker.client.table(tracker.table)
             .update({
                 'closing_odds': closing,
                 'captured_closing_at': now.isoformat()
             }).eq('id', p['id']).execute())
            captured += 1
            logger.info(f"🔻 #{p['id']} cierre {closing:.2f} "
                        f"(tomada {p['cuota']:.2f}) → CLV {clv:+.1f}%")

    logger.info(f"✅ Capturas realizadas este run: {captured}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    args = ap.parse_args()
    sys.exit(run(test=args.test))
