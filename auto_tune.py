#!/usr/bin/env python3
"""
v0.5-A · Auto-ajuste operativo.
Lee los picks liquidados, mide el gap de calibración (EV declarado − yield real)
y escribe en Supabase (tabla meta, clave 'auto_tune') la configuración que
auto_scan debe aplicar: umbral de EV de notificación y fracción Kelly.

Reglas:
- gap > +25 pp  → sobreestima mucho → EV 15%, Kelly 1/8
- gap > +10 pp  → sobreestima       → EV 12%, Kelly 1/8
- gap +5..+10   → leve              → EV 11%, Kelly 1/6
- gap -5..+5    → calibrado         → EV 10%, Kelly 1/4 (default)
- gap < -5      → conservador       → EV 6%,  Kelly 1/2

Avisa por Telegram cuando cambia la configuración.
Si se ejecuta MANUALMENTE (Actions → Run workflow), envía siempre el estado
actual aunque no haya cambios, para consultarlo bajo demanda.
"""
import os
import sys
import json
import logging
import requests
from datetime import datetime, timezone, timedelta

from stats_tracker import StatsTracker
from reports import compute_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

MIN_SAMPLE = 20
META_KEY = 'auto_tune'

DEFAULTS = {'ev_notify': 10.0, 'kelly': 4}


def send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"❌ Telegram: {e}")
        return False


def decide(gap):
    if gap > 25:
        return {'ev_notify': 15.0, 'kelly': 8}, "sobreestimación muy fuerte"
    if gap > 10:
        return {'ev_notify': 12.0, 'kelly': 8}, "sobreestimación fuerte"
    if gap > 5:
        return {'ev_notify': 11.0, 'kelly': 6}, "sobreestimación leve"
    if gap < -5:
        return {'ev_notify': 6.0, 'kelly': 2}, "modo conservador"
    return dict(DEFAULTS), "calibrado"


def main():
    manual = os.getenv('GITHUB_EVENT_NAME') == 'workflow_dispatch'

    tracker = StatsTracker()
    if not tracker.enabled:
        logger.error("❌ Supabase no configurado")
        return 1

    all_picks = tracker.get_all_picks()
    s = compute_for(all_picks)

    # 🧪 Gap reciente: solo picks GENERADOS en los últimos 14 días
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent = []
    for p in all_picks:
        if p.get('status') not in ('won', 'lost'):
            continue
        try:
            t = datetime.fromisoformat((p.get('timestamp') or '').replace('Z', '+00:00'))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= cutoff:
                recent.append(p)
        except Exception:
            continue
    sr = compute_for(recent) if recent else None

    if s['settled'] < MIN_SAMPLE or s['gap'] is None:
        if manual:
            send(f"🤖 <b>ESTADO DEL AUTO-AJUSTE</b>\n\n"
                 f"📊 Muestra insuficiente ({s['settled']}/{MIN_SAMPLE} liquidados).\n"
                 f"⚙️ Config por defecto: EV≥10% · Kelly 1/4\n\n"
                 f"ℹ️ Más info: /glosario")
        else:
            logger.info(f"Muestra insuficiente ({s['settled']}/{MIN_SAMPLE}) o sin gap. Mantengo defaults.")
        return 0

    gap = s['gap']
    new_cfg, motivo = decide(gap)

    # Leer configuración previa para detectar cambio
    prev = dict(DEFAULTS)
    try:
        resp = tracker.client.table('meta').select('value').eq('key', META_KEY).execute()
        if resp.data:
            prev = json.loads(resp.data[0]['value']).get('cfg', prev)
    except Exception as e:
        logger.debug(f"No hay config previa: {e}")

    changed = (prev.get('ev_notify') != new_cfg['ev_notify']) or (prev.get('kelly') != new_cfg['kelly'])

    payload = {
        'key': META_KEY,
        'value': json.dumps({
            'cfg': new_cfg,
            'gap': round(gap, 1),
            'n': s['settled'],
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
    }
    tracker.client.table('meta').upsert(payload, on_conflict='key').execute()
    logger.info(f"🤖 Auto-ajuste guardado: EV≥{new_cfg['ev_notify']}% Kelly 1/{new_cfg['kelly']} "
                f"(gap {gap:+.1f} pp, n={s['settled']})")

    if changed or manual:
        # Detectar transición específica: sale del modo seguridad (gap >+10 → ≤+10)
        saliendo_de_seguridad = (
            changed
            and prev.get('ev_notify', 10) >= 12.0
            and new_cfg['ev_notify'] < 12.0
        )
        if not changed:
            cabecera = "🤖 <b>ESTADO DEL AUTO-AJUSTE</b> (sin cambios)"
            lectura = "✅ Sin cambios: se mantiene la configuración actual"
        elif saliendo_de_seguridad:
            cabecera = "🟢 <b>MODO SEGURIDAD DESACTIVADO</b>"
            lectura = (
                "El gap ha bajado por debajo de +10 pp.\n"
                "Vuelven las apuestas con stake.\n"
                "Si el gap vuelve a subir de +10, el sistema se protege solo."
            )
        elif new_cfg['ev_notify'] > prev.get('ev_notify', 10):
            cabecera = "🤖 <b>AUTO-AJUSTE DEL SISTEMA</b>"
            lectura = "📈 IA más afinada: subo el listón de calidad y protejo banca"
        elif new_cfg['ev_notify'] < prev.get('ev_notify', 10):
            cabecera = "🤖 <b>AUTO-AJUSTE DEL SISTEMA</b>"
            lectura = "📉 IA conservadora: bajo el listón para no perder oportunidades"
        else:
            cabecera = "🤖 <b>AUTO-AJUSTE DEL SISTEMA</b>"
            lectura = "🔄 Ajuste de Kelly según calibración detectada"

        if sr and sr.get('gap') is not None and sr['settled'] >= 5:
            linea_reciente = (f"🧪 Gap reciente (14 días, n={sr['settled']}): "
                              f"<b>{sr['gap']:+.1f} pp</b>\n")
        else:
            linea_reciente = "🧪 Gap reciente: muestra aún insuficiente\n"

        msg = (f"{cabecera}\n\n"
               f"⚖️ Gap: <b>{gap:+.1f} pp</b> ({motivo})\n"
               f"🎯 EV mínimo: {prev.get('ev_notify', 10):.0f}% → <b>{new_cfg['ev_notify']:.0f}%</b>\n"
               f"💰 Kelly: 1/{prev.get('kelly', 4)} → <b>1/{new_cfg['kelly']}</b>\n"
               f"📊 Muestra: {s['settled']} liquidados\n"
               f"{linea_reciente}\n"
               f"<i>{lectura}</i>\n\n"
               f"ℹ️ Más info: /glosario")
        send(msg)
        logger.info("📨 Aviso de auto-ajuste enviado")
    else:
        logger.info("Sin cambios de configuración respecto al ajuste anterior.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
