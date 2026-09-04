#!/usr/bin/env python3
"""
Liquidación masiva de picks pendientes con múltiples fuentes:
1º ESPN (gratis, amplio)
2º football-data.co.uk (gratis, ligas UK/ESC)
3º TheSportsDB (gratis, ligas asiáticas/exóticas)
4º VOID automático a los 7 días (nunca atascos)
"""
import os
import sys
import time
import csv
import io
import json
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

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard"

# Mapa: patrón (liga normalizada de The Odds API) → slug ESPN
# RUSIA ANTES que 'premier league' genérico
LEAGUE_MAP = [
    # Países con "Premier League" propia ANTES que el genérico 'premier league'
    ('russia', 'rus.1'),
    ('russian', 'rus.1'),
    ('ukraine', 'ukr.1'),
    # Corea ANTES que 'league 1' de Inglaterra
    ('k league', 'kor.1'),
    ('south korea', 'kor.1'),
    # Brasil ANTES que 'serie a/b' de Italia
    ('brazil serie b', 'bra.2'),
    ('serie b - brazil', 'bra.2'),
    ('brazil serie a', 'bra.1'),
    ('serie a - brazil', 'bra.1'),
    ('brazil', 'bra.1'),
    # Inglaterra / Escocia
    ('scotland championship', 'sco.2'),
    ('england championship', 'eng.2'),
    ('championship', 'eng.2'),
    ('england league one', 'eng.3'),
    ('league one', 'eng.3'),
    ('league 1', 'eng.3'),
    ('england league two', 'eng.4'),
    ('league two', 'eng.4'),
    ('england premier', 'eng.1'),
    ('premier league', 'eng.1'),
    ('premiership', 'sco.1'),
    ('scotland', 'sco.1'),
    # España
    ('la liga 2', 'esp.2'),
    ('la liga', 'esp.1'),
    # Alemania / Austria (Austria antes del genérico)
    ('austrian', 'aut.1'),
    ('austria bundesliga', 'aut.1'),
    ('3 liga', 'ger.3'),
    ('bundesliga 2', 'ger.2'),
    ('2. bundesliga', 'ger.2'),
    ('germany bundesliga', 'ger.1'),
    ('bundesliga', 'ger.1'),
    # Italia (después de Brasil)
    ('serie b', 'ita.2'),
    ('serie a', 'ita.1'),
    ('coppa italia', 'ita.coppa_italia'),
    # Francia
    ('ligue 2', 'fra.2'),
    ('ligue 1', 'fra.1'),
    # Países Bajos / Portugal
    ('eerste divisie', 'ned.2'),
    ('eredivisie', 'ned.1'),
    ('primeira liga', 'por.1'),
    ('portugal', 'por.1'),
    # Nórdicas
    ('eliteserien', 'nor.1'),
    ('obos', 'nor.2'),
    ('allsvenskan', 'swe.1'),
    ('superettan', 'swe.2'),
    ('superliga', 'den.1'),
    ('veikkausliiga', 'fin.1'),
    ('ykkonen', 'fin.2'),
    ('urvalsdeild', 'ice.1'),
    # Suiza (sui.1) / Bélgica
    ('swiss', 'sui.1'),
    ('superleague', 'sui.1'),
    ('belgium', 'bel.1'),
    # Resto Europa
    ('turkey', 'tur.1'),
    ('greece', 'gre.1'),
    ('ekstraklasa', 'pol.ekstraklasa'),
    ('poland', 'pol.ekstraklasa'),
    ('czech', 'cze.1'),
    ('croatia', 'cro.1'),
    ('estonia', 'est.1'),
    ('romania', 'rou.1'),
    ('hungary', 'hun.1'),
    ('serbia', 'srb.1'),
    ('bulgaria', 'bul.1'),
    # Asia / Oceanía
    ('saudi', 'ksa.1'),
    ('j league', 'jpn.1'),
    ('japan', 'jpn.1'),
    ('china', 'chn.1'),
    ('australia', 'aus.1'),
    # América
    ('usa mls', 'usa.1'),
    ('mls', 'usa.1'),
    ('mexico', 'mex.1'),
    ('argentina', 'arg.1'),
    ('chile', 'chi.1'),
    ('colombia', 'col.1'),
    ('uruguay', 'uru.1'),
    ('paraguay', 'par.1'),
    ('ecuador', 'ecu.1'),
    ('peru', 'per.1'),
    ('bolivia', 'bol.1'),
    ('venezuela', 'ven.1'),
    # Copas
    ('libertadores', 'conmebol.libertadores'),
    ('sudamericana', 'conmebol.sudamericana'),
    ('champions league', 'uefa.champions'),
    ('europa league', 'uefa.europa'),
]

# Alias de nombres de equipo (The Odds API → ESPN)
TEAM_ALIASES = {
    'hearts': 'heart of midlothian',
}


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


def league_to_slug(liga):
    nl = norm(liga)
    for pattern, slug in LEAGUE_MAP:
        if pattern in nl:
            return slug
    return None


# ==========================================
# FUENTE 1: ESPN
# ==========================================
def get_scoreboard(slug, date_str, cache):
    key = (slug, date_str)
    if key in cache:
        return cache[key]
    events = []
    try:
        r = requests.get(ESPN.format(slug),
                         params={"dates": date_str.replace('-', '')}, timeout=15)
        if r.status_code == 200:
            events = r.json().get("events") or []
            logger.info(f"📡 ESPN {slug} · {date_str}: {len(events)} eventos")
        else:
            logger.warning(f"⚠️ ESPN {slug} · {date_str}: HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"❌ ESPN {slug} · {date_str}: {e}")
    cache[key] = events
    time.sleep(0.6)
    return events


def extract_match(ev):
    """Devuelve (home, away, score_home, score_away, completed) o None."""
    try:
        comp = ev["competitions"][0]
        home = away = None
        for c in comp["competitors"]:
            team_name = (c.get("team", {}).get("displayName")
                         or c.get("team", {}).get("name") or "")
            score = c.get("score")
            if c.get("homeAway") == "home":
                home = (team_name, score)
            elif c.get("homeAway") == "away":
                away = (team_name, score)
        completed = ev.get("status", {}).get("type", {}).get("completed", False)
        if not home or not away:
            return None
        return home[0], away[0], home[1], away[1], completed
    except Exception:
        return None


def tnorm(s):
    n = norm(s)
    return TEAM_ALIASES.get(n, n)


def find_in_events(events, home, away):
    nh, na = tnorm(home), tnorm(away)
    best, best_score = None, 0.0
    for ev in events:
        m = extract_match(ev)
        if not m:
            continue
        s = sim(nh, tnorm(m[0])) + sim(na, tnorm(m[1]))
        if s > best_score:
            best_score, best = s, m
    return best if best_score >= 1.6 else None


# ==========================================
# FUENTE 2: football-data.co.uk
# ==========================================
FD_LEAGUE_MAP = [
    ('league 2', 'E3'),
    ('league two', 'E3'),
    ('league 1', 'E2'),
    ('league one', 'E2'),
    ('championship', 'E1'),
    ('scotland championship', 'SC1'),
]

def league_to_fd(liga):
    nl = norm(liga)
    for pattern, code in FD_LEAGUE_MAP:
        if pattern in nl:
            return code
    return None

def fd_seasons():
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    start = y % 100 if m >= 7 else (y - 1) % 100
    cur = f"{start:02d}{(start + 1) % 100:02d}"
    prev = f"{(start - 1) % 100:02d}{start:02d}"
    return [cur, prev]

def get_fd_rows(code, cache):
    if code in cache:
        return cache[code]
    rows = []
    for season in fd_seasons():
        url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                continue
            for row in csv.DictReader(io.StringIO(r.text)):
                if row.get('Date') and row.get('HomeTeam') and row.get('AwayTeam'):
                    rows.append(row)
        except Exception as e:
            logger.warning(f"⚠️ football-data {season}/{code}: {e}")
    cache[code] = rows
    time.sleep(0.5)
    return rows

def parse_fd_date(s):
    for fmt in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None

def find_in_fd(rows, home, away, d):
    nh, na = tnorm(home), tnorm(away)
    best, best_score = None, 0.0
    for row in rows:
        rd = parse_fd_date(row['Date'])
        if rd is None or abs((rd - d).days) > 2:
            continue
        s = sim(nh, tnorm(row['HomeTeam'])) + sim(na, tnorm(row['AwayTeam']))
        if s > best_score:
            best_score, best = s, row
    if best is None or best_score < 1.6:
        return None
    try:
        return int(best['FTHG']), int(best['FTAG'])
    except Exception:
        return None


# ==========================================
# FUENTE 3: TheSportsDB
# ==========================================
TSD_API = "https://www.thesportsdb.com/api/v1/json/3/searchevents.php"

def find_in_thesportsdb(home, away, d, cache):
    key = (home, away, d.isoformat())
    if key in cache:
        return cache[key]
    
    # Buscar por nombres ORIGINALES (TheSportsDB distingue mayúsculas)
    query = f"{home}_vs_{away}"
    params = {"e": query}
    logger.info(f"🌐 TheSportsDB buscando: {query}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        r = requests.get(TSD_API, params=params, headers=headers, timeout=15)
        logger.info(f"🌐 TheSportsDB status: {r.status_code}, length: {len(r.text)}")
        
        if r.status_code != 200:
            logger.warning(f"⚠️ TheSportsDB HTTP {r.status_code}")
            cache[key] = None
            return None
        
        data = r.json()
        events = data.get("events") or []
        logger.info(f"🌐 TheSportsDB devolvió {len(events)} eventos")
        
        if not events:
            logger.info(f"🌐 TheSportsDB: sin eventos para '{query}'")
            cache[key] = None
            return None
        
        best, best_score = None, 0.0
        for ev in events:
            ev_date = ev.get("dateEvent")
            if not ev_date:
                continue
            try:
                ev_d = datetime.strptime(ev_date, "%Y-%m-%d").date()
            except Exception:
                continue
            
            if abs((ev_d - d).days) > 2:
                continue
            
            ev_home = ev.get("strHomeTeam", "")
            ev_away = ev.get("strAwayTeam", "")
            
            # TheSportsDB a veces invierte home/away
            s1 = sim(home.lower(), ev_home.lower()) + sim(away.lower(), ev_away.lower())
            s2 = sim(home.lower(), ev_away.lower()) + sim(away.lower(), ev_home.lower())
            s = max(s1, s2)
            
            if s > best_score:
                best_score = s
                best = ev
        
        if best is None or best_score < 1.6:
            logger.info(f"🌐 TheSportsDB: mejor score {best_score:.2f} < 1.6")
            cache[key] = None
            return None
        
        # Verificar que el partido terminó
        status = best.get("strStatus", "")
        if status not in ("FT", "AET", "AP"):
            logger.info(f"🌐 TheSportsDB: partido no terminado (status: {status})")
            cache[key] = None
            return None
        
        try:
            hg = int(best.get("intHomeScore") or 0)
            ag = int(best.get("intAwayScore") or 0)
            
            # Si el pick tenía home/away invertidos, invertir marcador
            if s2 > s1:
                hg, ag = ag, hg
            
            logger.info(f"✅ TheSportsDB: encontrado {hg}-{ag}")
            cache[key] = (hg, ag)
            return (hg, ag)
        except Exception as e:
            logger.warning(f"⚠️ TheSportsDB parse error: {e}")
            cache[key] = None
            return None
            
    except Exception as e:
        logger.warning(f"⚠️ TheSportsDB exception: {e}")
        cache[key] = None
        return None

# ==========================================
# EVALUACIÓN Y LÓGICA PRINCIPAL
# ==========================================
def parse_pick_date(hora):
    try:
        date_part = hora.split()[0]
        day, month = date_part.split('/')
        today = datetime.now(timezone.utc).date()
        d = datetime(today.year, int(month), int(day)).date()
        if d > today + timedelta(days=1):
            d = datetime(today.year - 1, int(month), int(day)).date()
        return d.isoformat(), d
    except Exception:
        return None, None


def evaluate_pick(pick, hg, ag):
    mercado = (pick.get('mercado') or '').lower()
    total = hg + ag

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
    logger.info("🚀 Iniciando liquidación masiva (ESPN)...")

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
    fd_cache = {}
    tsd_cache = {}
    unmapped = set()
    settled = won = lost = void = skipped = not_found = future = 0

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
        if d >= datetime.now(timezone.utc).date():
            future += 1
            continue
        aged = (datetime.now(timezone.utc).date() - d).days

        slug = league_to_slug(pick.get('liga', ''))
        fd_code = league_to_fd(pick.get('liga', ''))
        if not slug and not fd_code:
            unmapped.add(pick.get('liga', '?'))
            if aged > 7:
                tracker.settle_pick(pick['id'], 'void')
                void += 1
                logger.info(f"➖ {partido}: liga sin mapear y >7 días → VOID")
            else:
                skipped += 1
            continue

        m = None
        if slug:
            for delta in (0, -1, 1):
                d2 = d + timedelta(days=delta)
                events = get_scoreboard(slug, d2.isoformat(), cache)
                m = find_in_events(events, home, away)
                if m:
                    if delta != 0:
                        logger.info(f"📅 {partido}: encontrado con desfase de {delta:+d} día(s)")
                    break
        
        if not m and fd_code:
            fd_res = find_in_fd(get_fd_rows(fd_code, fd_cache), home, away, d)
            if fd_res is not None:
                hg, ag = fd_res
                status = evaluate_pick(pick, hg, ag)
                if status is not None:
                    tracker.settle_pick(pick['id'], status)
                    settled += 1
                    if status == 'won':
                        won += 1
                        logger.info(f"✅ {partido}: WON ({hg}-{ag}) [football-data]")
                    elif status == 'lost':
                        lost += 1
                        logger.info(f"❌ {partido}: LOST ({hg}-{ag}) [football-data]")
                    else:
                        void += 1
                        logger.info(f"➖ {partido}: VOID [football-data]")
                    continue
        
        if not m:
            tsd_res = find_in_thesportsdb(home, away, d, tsd_cache)
            if tsd_res is not None:
                hg, ag = tsd_res
                status = evaluate_pick(pick, hg, ag)
                if status is not None:
                    tracker.settle_pick(pick['id'], status)
                    settled += 1
                    if status == 'won':
                        won += 1
                        logger.info(f"✅ {partido}: WON ({hg}-{ag}) [TheSportsDB]")
                    elif status == 'lost':
                        lost += 1
                        logger.info(f"❌ {partido}: LOST ({hg}-{ag}) [TheSportsDB]")
                    else:
                        void += 1
                        logger.info(f"➖ {partido}: VOID [TheSportsDB]")
                    continue

        if not m:
            if aged > 7:
                tracker.settle_pick(pick['id'], 'void')
                void += 1
                logger.info(f"➖ {partido}: sin resultado en ESPN y >7 días → VOID")
            else:
                not_found += 1
                logger.info(f"⏳ {partido} ({slug} · {date_str}): sin coincidencia")
            continue

        eh, ea, sh, sa, completed = m
        if not completed or sh is None or sa is None or str(sh).strip() == '' or str(sa).strip() == '':
            not_found += 1
            continue
        hg, ag = int(float(sh)), int(float(sa))

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

    if unmapped:
        logger.info(f"️ Ligas sin mapear ESPN ({len(unmapped)}): {sorted(unmapped)}")

    summary = (f"🤖 <b>LIQUIDACIÓN MASIVA COMPLETADA</b>\n\n"
               f"📊 Liquidados: {settled} (✅ {won} · ❌ {lost} · ➖ {void})\n"
               f"⏳ Sin resultado: {not_found} · Futuros: {future}\n"
               f"⏭️ Omitidos (HT/liga sin mapear): {skipped}\n\n"
               f"💡 Usa /stats para ver el rendimiento actualizado.")
    send_telegram(summary)
    logger.info(f"✅ Liquidación completada: {settled} procesados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
