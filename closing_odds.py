#!/usr/bin/env python3
"""
v0.4-D · Captura de cuota de cierre para el cálculo de CLV.
Para picks pendientes con kickoff en las próximas WINDOW_HOURS,
consulta The Odds API y guarda la mejor cuota actual como closing_odds.

CLV = (cuota_tomada / cuota_cierre - 1) * 100 → positivo = bates al mercado.
Mercados con cierre: 1X2, Over FT y Over 1ª Parte. (BTTS/DC quedan sin CLV en v1.)
Modo --test: calcula y loguea sin guardar (para verificar sin ensuciar datos).

Ahora usa odds_client.py para rotación de claves y contador mensual.
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

WINDOW_HOURS = 6.0
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
        logger.error(f"Error listando deportes (todas las claves agotadas)")
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
    # Mercados de 1ª Parte no tienen endpoint de cierre fiable en The Odds API v4.
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
    return None  # BTTS / Doble Oportunidad → sin CLV en v1


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
    for p in tracker.get_all_picks():
        if p['status'] != 'pending' or p.get('closing_odds'):
            continue
        dt = pick_match_dt(p)
        if not dt:
            continue
        if test:
            if dt > now_madrid:
                cand.append(p)
        elif timedelta(0) <= (dt - now_madrid) <= timedelta(hours=WINDOW_HOURS):
            cand.append(p)

    logger.info(f"🔎 Picks en ventana de cierre: {len(cand)}")
    if not cand:
        return 0

    sports_map = get_sports_map(tracker)

    by_league = {}
    for p in cand:
        by_league.setdefault(p['liga'], []).append(p)

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

            tracker.client.table(tracker.table).update({
                'closing_odds': closing,
                'captured_closing_at': now.isoformat()
            }).eq('id', p['id']).execute()
            logger.info(f"🔻 #{p['id']} cierre {closing:.2f} (tomada {p['cuota']:.2f}) → CLV {clv:+.1f}%")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    args = ap.parse_args()
    sys.exit(run(test=args.test))
