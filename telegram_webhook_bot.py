#!/usr/bin/env python3
"""
v0.3-A · Bot de comandos de Telegram por WEBHOOK (microservicio Render).
Comandos: /stats /week /today /market <texto> /help
Solo responde al chat TELEGRAM_CHAT_ID (seguridad).
El webhook se registra solo al arrancar (usa RENDER_EXTERNAL_URL).
"""
import os
import logging
import requests
import unicodedata
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify

from stats_tracker import StatsTracker
from reports import compute_for, fmt_stats, fmt_weekly, picks_settled_since

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = str(os.getenv('TELEGRAM_CHAT_ID', ''))

app = Flask(__name__)

HELP = """
🤖 <b>Comandos disponibles</b>

/stats → rendimiento acumulado
/week → resumen de los últimos 7 días
/today → picks registrados hoy
/market BTTS → rendimiento de un mercado
/market → lista de mercados con datos
/help → este menú
""".strip()


def _norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def tg(method, **data):
    try:
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                             json=data, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return None


@app.route('/')
def health():
    return jsonify(ok=True, service="valuebets-bot")


@app.route('/telegram', methods=['POST'])
def telegram():
    update = request.get_json(silent=True) or {}
    message = update.get('message') or update.get('edited_message') or {}
    chat_id = str((message.get('chat') or {}).get('id', ''))
    text = (message.get('text') or '').strip()

    logger.info(f"📨 Recibido: chat_id={chat_id}, text={text}, CHAT_ID={CHAT_ID}")

    # Solo tu chat y solo comandos
    if chat_id != CHAT_ID or not text.startswith('/'):
        return jsonify(ok=True)

    reply = handle(text)
    if reply:
        tg('sendMessage', chat_id=CHAT_ID, text=reply, parse_mode='HTML')
    return jsonify(ok=True)


def handle(text):
    parts = text.split()
    cmd = parts[0].lower().split('@')[0]

    if cmd in ('/start', '/help'):
        return HELP

    tracker = StatsTracker()
    if not tracker.enabled:
        return "⚠️ Estadísticas no configuradas (Supabase)"

    all_picks = tracker.get_all_picks()

    if cmd == '/stats':
        return fmt_stats(compute_for(all_picks), "RENDIMIENTO ACUMULADO")

    if cmd == '/week':
        return fmt_weekly(picks_settled_since(tracker, 7), compute_for(all_picks))

    if cmd == '/today':
        today = datetime.now(timezone.utc).date().isoformat()
        todays = [p for p in all_picks if (p.get('timestamp') or '').startswith(today)]
        if not todays:
            return "💤 Hoy no se han registrado picks todavía."
        L = [f"📅 <b>Picks de hoy ({len(todays)})</b>", ""]
        for p in todays[:15]:
            st_ = {'pending': '⏳', 'won': '✅', 'lost': '❌', 'void': '➖'}.get(p['status'], '⏳')
            cuota = p['cuota'] or 0
            ev = p['ev_percentage'] or 0
            L.append(f"{st_} {p['partido']}\n   {p['mercado']} @ {cuota:.2f} · EV {ev:+.1f}%")
        return "\n".join(L)

    if cmd == '/market':
        q = _norm(' '.join(parts[1:])) if len(parts) > 1 else ''
        if not q:
            mkts = sorted({(p['mercado'] or '') for p in all_picks if p['mercado']})
            return "📊 <b>Mercados con datos</b>\n" + "\n".join(f"· {m}" for m in mkts)
        sel = [p for p in all_picks if q in _norm(p['mercado'])]
        if not sel:
            return f"🤷 Sin picks para «{' '.join(parts[1:])}»"
        return fmt_stats(compute_for(sel), f"MERCADO: {' '.join(parts[1:]).upper()}")

    return HELP


# ==========================================
# Registro del webhook en thread separado (no bloqueante)
# ==========================================
def _register_webhook():
    base = os.getenv('WEBHOOK_URL', '').strip()
    if not base:
        ext = os.getenv('RENDER_EXTERNAL_URL', '').strip().rstrip('/')
        if ext:
            base = f"{ext}/telegram"
    if base and BOT_TOKEN:
        r = tg('setWebhook', url=base)
        logger.info(f"🔗 Webhook: {base} → HTTP {r.status_code if r is not None else 'ERROR'}")


# Registrar webhook en thread separado para no bloquear el arranque
threading.Thread(target=_register_webhook, daemon=True).start()
