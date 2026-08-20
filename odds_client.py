#!/usr/bin/env python3
"""
Cliente The Odds API con rotación de claves y contador mensual.
- Prueba THE_ODDS_API_KEY, _2 y _3 en orden.
- Si una clave falla por cuota/auth, rota a la siguiente.
- Cuenta requests en Supabase (meta, clave 'odds_usage') con tope mensual.
"""
import os
import json
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Tope mensual global (suma de las 3 claves, con reserva de seguridad)
MONTHLY_CAP = int(os.getenv('ODDS_MONTHLY_CAP', '1350'))


def get_keys():
    return [k for k in (os.getenv('THE_ODDS_API_KEY'),
                        os.getenv('THE_ODDS_API_KEY_2'),
                        os.getenv('THE_ODDS_API_KEY_3')) if k]


def _read_usage(tracker):
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    try:
        resp = tracker.client.table('meta').select('value').eq('key', 'odds_usage').execute()
        if resp.data:
            data = json.loads(resp.data[0]['value'])
            if data.get('month') == month:
                return month, data.get('count', 0)
    except Exception:
        pass
    return month, 0


def _bump_usage(tracker, month, count):
    try:
        tracker.client.table('meta').upsert(
            {'key': 'odds_usage',
             'value': json.dumps({'month': month, 'count': count + 1})},
            on_conflict='key').execute()
    except Exception:
        pass


def odds_get(path, params=None, tracker=None):
    """GET a The Odds API v4 con rotación y contador. Devuelve Response o None."""
    keys = get_keys()
    if not keys:
        logger.error("❌ No hay claves de The Odds API configuradas")
        return None

    month, count = _read_usage(tracker) if tracker else (None, 0)
    if tracker and count >= MONTHLY_CAP:
        logger.warning(f"🚫 Tope mensual alcanzado ({count}/{MONTHLY_CAP}): no se consulta")
        return None

    last = None
    for i, key in enumerate(keys):
        p = dict(params or {})
        p['apiKey'] = key
        try:
            r = requests.get(f"https://api.the-odds-api.com/v4/{path}", params=p, timeout=15)
        except Exception as e:
            logger.error(f"❌ Odds API error de red: {e}")
            continue
        last = r
        if r.status_code == 200:
            if tracker:
                _bump_usage(tracker, month, count)
            logger.info(f"🔑 Odds API clave #{i+1} OK (uso del mes: {count + 1})")
            return r
        if r.status_code in (401, 403, 429) or 'OUT_OF_USAGE' in r.text[:200]:
            logger.warning(f"⚠️ Odds API clave #{i+1} agotada/no autorizada → rotando")
            continue
        logger.error(f"❌ Odds API HTTP {r.status_code}: {r.text[:200]}")
        return r

    logger.error("❌ Todas las claves de Odds API agotadas")
    return last
