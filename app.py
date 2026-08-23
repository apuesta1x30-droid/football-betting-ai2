import streamlit as st
import pandas as pd
import numpy as np
import requests
import joblib
import warnings
import plotly.graph_objects as go
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from scipy.stats import poisson
import os
warnings.filterwarnings('ignore')

from stats_tracker import StatsTracker

st.set_page_config(page_title="⚽ Multi-Mercado Value Bet Scanner", layout="wide")

# ==========================================
# GLOSARIO (se abre con ?glosario=1 desde Telegram)
# ==========================================
_glosario = False
try:
    _glosario = st.query_params.get("glosario") == "1"
except Exception:
    _glosario = False

if _glosario:
    st.title("📚 Glosario de métricas")
    st.caption("Guía completa para interpretar las estadísticas del sistema")
    
    st.markdown("## 🎯 Métricas de una alerta")
    st.markdown("""
    - **EV (%)** — Valor esperado: `(Prob. IA × cuota) − 1`. Es el margen que el sistema cree que tienes. ✅ Favorable: cuanto más alto, mejor (el sistema solo notifica EV ≥ 10% por defecto).
    - **Prob. IA vs Prob. Casa** — La probabilidad que estima el modelo frente a la que implica la cuota (`1/cuota`). Si la IA dice más que la casa, hay value.
    - **Stake (Kelly 1/X)** — % de banca sugerido. El sistema usa Kelly fraccionado para proteger la banca: cuanto mayor fracción (1/8), más conservador.
    """)
    
    st.markdown("## 📊 Métricas de resultado")
    st.markdown("""
    - **Liquidados (✅/❌)** — Picks con resultado cerrado. ✅ = acierto, ❌ = fallo, ➖ = anulado.
    - **Hit rate** — % de aciertos. **Solo no basta**: depende de la cuota media a la que apuestas.
    - **PnL (unidades)** — Beneficio contando 1 unidad por pick. ✅ Favorable: > 0.
    - **Yield** — `PnL / apuestas`. La rentabilidad real. Es la métrica reina a largo plazo.
    """)
    
    st.markdown("### 🎯 Hit rate necesario según tu cuota media")
    st.markdown("""
    | Cuota media | Hit para no perder |
    |---|---|
    | 1.50 | 66.7% |
    | 1.80 | 55.6% |
    | 2.00 | 50.0% |
    | 2.50 | 40.0% |
    | 3.00 | 33.3% |
    """)
    
    st.markdown("### 📈 Rangos de yield")
    st.markdown("""
    | Yield | Lectura |
    |---|---|
    | < 0% | En pérdidas |
    | 0–5% | Positivo modesto |
    | 5–10% | Bueno (nivel profesional sostenido) |
    | > 10% | Excelente; en pocas semanas suele ser varianza |
    """)
    
    st.markdown("## 🔻 Métricas de mercado (CLV)")
    st.markdown("""
    - **CLV (Closing Line Value)** — `(cuota tomada / cuota de cierre − 1)`. Mide si tu cuota fue mejor que la del cierre del mercado. ✅ Favorable: > 0.
    - **Bate al cierre** — % de picks cuya cuota batió al cierre. ✅ Favorable: > 50%.
    - El CLV es el **mejor predictor de rentabilidad futura**: puedes perder dinero a corto plazo y aun así estar jugando bien si tu CLV es positivo.
    """)
    
    st.markdown("### 🧠 Cómo leer PnL y CLV juntos")
    st.markdown("""
    | PnL | CLV | Lectura |
    |---|---|---|
    | ✅ + | ✅ + | Edge real: sistema rentable y validado por el mercado |
    | ✅ + | 🔻 − | Ganas sin batir al mercado: posible suerte a corto. Vigilar |
    | ❌ − | ✅ + | Pierdes pero eliges bien: señal positiva a largo plazo |
    | ❌ − | 🔻 − | Revisar modelo y umbrales |
    """)
    
    st.markdown("## 🤖 Métricas del modelo")
    st.markdown("""
    - **Brier** — Error medio de las probabilidades de la IA (menor = mejor). ✅ < 0.20 bueno · < 0.15 muy bueno.
    - **Gap de calibración** — Prob. IA prometida − acierto real (en puntos). ✅ Favorable: entre −5 y +5.
      - Gap > +10: la IA sobreestima → el sistema sube solo el EV mínimo (auto-ajuste).
      - Gap < −10: la IA es conservadora → el sistema baja el EV mínimo.
    """)
    
    st.markdown("## 📖 Ejemplo real: tu semana 09/08–16/08")
    st.markdown("""
    | Dato | Valor | Lectura |
    |---|---|---|
    | Liquidados | 43 (21✅/22❌) | Muestra aún pequeña |
    | Hit rate | 48.8% | Correcto si tu cuota media ronda 2.0 o más |
    | PnL / Yield | +9.79 u / +22.8% | Semana excelente; con 43 picks es varianza: no esperes sostenerlo |
    | CLV | −3.8% · bate 27% | 🔻 El mercado cerró por encima de tus cuotas |
    
    **Lectura combinada**: PnL ✅ + CLV 🔻 → estás ganando dinero pero sin batir al mercado.
    Con 43 picks es normal (varianza). La señal de alarma solo aparece si tras 100–200 picks
    el CLV sigue negativo mientras ganas: entonces el modelo no tendría edge real.
    """)
    
    st.markdown("## ✅ Reglas rápidas de decisión")
    st.markdown("""
    1. **Yield > 0 y CLV > 0** con 100+ picks → el sistema funciona: mantén el rumbo.
    2. **CLV negativo sostenido** → revisa umbrales de EV y calibración.
    3. **Gap de calibración > +10** → el auto-ajuste ya está subiendo tu EV mínimo: confía en él.
    4. **Hit rate por debajo de 1/cuota media** → estás perdiendo dinero aunque haya semanas verdes.
    """)
    st.markdown("## 🤖 Auto-ajuste del sistema (Capa A)")
    st.markdown("""
    Cada día a las 06:00 UTC, el sistema mide su **gap de calibración** (Prob. IA prometida − acierto real)
    con los picks liquidados y ajusta solo dos palancas: el **EV mínimo de notificación** y la **fracción Kelly**.
    Se activa con ≥20 picks liquidados. Cuando cambia la configuración, recibes un aviso en Telegram.
    """)
    
    st.markdown("### Estados y umbrales aplicados")
    st.markdown("""
    | Gap de calibración | Estado | EV mínimo | Kelly | Qué significa |
    |---|---|---|---|---|
    | > +10 pp | Sobreestima fuerte | 12% | 1/8 | La IA promete más de lo que cumple: máxima exigencia y banca protegida |
    | +5 a +10 pp | Sobreestima leve | 11% | 1/6 | Ligera inflación de probabilidades: algo más exigente |
    | −5 a +5 pp | Calibrado | 10% | 1/4 | La IA predice lo que ocurre: configuración estándar |
    | < −5 pp | Conservadora | 6% | 1/2 | La IA promete menos de lo que cumple: más oportunidades y stake mayor |
    """)
    
    st.markdown("### Cómo leer el aviso de Telegram")
    st.markdown("""
    Ejemplo real: `Gap −1.5 pp (calibrado) · EV 6% → 10% · Kelly 1/2 → 1/4`
    
    Significa: antes la IA era conservadora (por eso EV 6% y Kelly 1/2); al acumular muestra,
    el gap entró en zona calibrada y el sistema volvió a los valores estándar:
    **exige más calidad (EV 10%) y protege más la banca (Kelly 1/4)**.
    
    **Regla general**:
    - EV mínimo que SUBE + Kelly que BAJA → el sistema confía en su calibración: se vuelve exigente y seguro.
    - EV mínimo que BAJA + Kelly que SUBE → el sistema detecta que se queda corto: aprovecha más oportunidades.
    """)

    st.markdown("### 🎯 Ventana de cuotas recomendada: 1.8 – 2.8")
    st.markdown("""
    El sistema filtra automáticamente los picks fuera de este rango:
    
    - **Cuotas < 1.8**: exigen hit rates > 62% para tener valor. El modelo recalibrado (Capa B) rara vez puede demostrar esa confianza con certeza, así que son descartadas para evitar señales falsas.
    - **Cuotas > 2.8**: alta varianza y típica zona de sobreestimación histórica del modelo. Se filtran para proteger la banca de rachas largas de pérdidas.
    
    **Zona viva (1.8-2.8)**: concentra picks con hit rates esperados del 42-60% y varianza contenida. Es el rango donde el modelo puede demostrar ventaja real sobre el mercado.
    
    💡 Puedes ajustar estos valores en el sidebar si quieres explorar otros rangos, pero la ventana 1.8-2.8 es la que el bot de Telegram usa por defecto.
    """)
    
    st.markdown("---")
    st.markdown("⚠️ Herramienta de análisis estadístico. Las apuestas conllevan riesgo. Apuesta con responsabilidad.")
    st.stop()

# ==========================================
# CONFIGURACIÓN
# ==========================================
# Recopilamos hasta 3 API keys de The Odds API para rotación automática
THE_ODDS_API_KEYS = [
    st.secrets.get("THE_ODDS_API_KEY", os.getenv("THE_ODDS_API_KEY", "")),
    st.secrets.get("THE_ODDS_API_KEY_2", os.getenv("THE_ODDS_API_KEY_2", "")),
    st.secrets.get("THE_ODDS_API_KEY_3", os.getenv("THE_ODDS_API_KEY_3", ""))
]
# Filtramos para quedarnos solo con las que tengan valor (no estén vacías)
THE_ODDS_API_KEYS = [key for key in THE_ODDS_API_KEYS if key]

API_FOOTBALL_KEY = st.secrets.get("API_FOOTBALL_KEY", os.getenv("API_FOOTBALL_KEY", ""))
API_FOOTBALL_HEADERS = {'x-rapidapi-key': API_FOOTBALL_KEY, 'x-rapidapi-host': "v3.football.api-sports.io"}

# ==========================================
# CARGA DE MODELOS Y DATOS
# ==========================================
@st.cache_resource(show_spinner="Cargando modelos de IA...")
def load_all_models():
    models = {}
    try:
        models['over15'] = joblib.load('model_over15.pkl')
        models['over25'] = joblib.load('model_over25.pkl')
        models['over35'] = joblib.load('model_over35.pkl')
        models['btts'] = joblib.load('model_btts.pkl')
        models['1x2'] = joblib.load('model_1x2.pkl')
        return models
    except Exception as e:
        st.error(f"Error cargando modelos: {e}")
        return None

@st.cache_resource(show_spinner="Cargando base de datos...")
def load_team_database():
    try:
        df_teams = pd.read_csv('team_stats_db.csv')
        return df_teams.set_index('Team').to_dict('index')
    except:
        return {}

# ==========================================
# ESCANEO DE MERCADOS
# ==========================================
@st.cache_data(ttl=1800, show_spinner="Escaneando mercados...")
def scan_all_markets():
    if not THE_ODDS_API_KEYS:
        st.error("❌ No hay API keys de The Odds API configuradas en los Secrets de Streamlit.")
        return []
    
    # Intentar con cada key hasta que una funcione
    for i, api_key in enumerate(THE_ODDS_API_KEYS):
        url = "https://api.the-odds-api.com/v4/sports/soccer/odds"
        params = {
            "regions": "eu,us",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
            "apiKey": api_key
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
            
            # Si la respuesta es exitosa, devolver los datos inmediatamente
            if response.status_code == 200:
                return response.json()
            
            # Detectar agotamiento de créditos (código 429 o mensaje de error específico)
            if response.status_code == 429 or "OUT_OF_USAGE_CREDITS" in response.text or "usage" in response.text.lower():
                st.warning(f"⚠️ API Key #{i+1} agotada. Probando con la siguiente...")
                continue
            
            # Otros errores de la API
            st.error(f"Error en The Odds API (Key #{i+1}): {response.status_code} - {response.text}")
            continue
            
        except requests.exceptions.RequestException as e:
            st.warning(f"⚠️ Error de conexión con Key #{i+1}: {e}. Probando la siguiente...")
            continue
        except Exception as e:
            st.warning(f"⚠️ Error inesperado con Key #{i+1}: {e}. Probando la siguiente...")
            continue
            
    # Si el bucle termina, significa que todas fallaron
    st.error("❌ Todas las API keys de The Odds API están agotadas o han fallado. Revisa tus límites o añade más keys.")
    return []

def get_fixture_id_from_api_football(home_team, away_team, match_date):
    try:
        response = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers=API_FOOTBALL_HEADERS,
            params={"date": match_date[:10], "timezone": "UTC"},
            timeout=10
        )
        data = response.json().get("response", [])
        for fixture in data:
            h_team = fixture["teams"]["home"]["name"]
            a_team = fixture["teams"]["away"]["name"]
            if home_team.lower() in h_team.lower() and away_team.lower() in a_team.lower():
                return fixture["fixture"]["id"]
    except:
        pass
    return None

def get_api_football_odds(fixture_id):
    if not fixture_id:
        return {}
    try:
        response = requests.get(
            "https://v3.football.api-sports.io/odds",
            headers=API_FOOTBALL_HEADERS,
            params={"fixture": fixture_id},
            timeout=10
        )
        data = response.json().get("response", [])
        if not data:
            return {}
        odds_data = {}
        for item in data:
            for bookmaker in item.get("bookmakers", []):
                for bet in bookmaker.get("bets", []):
                    bet_id = bet["id"]
                    for value in bet.get("values", []):
                        if value["odd"]:
                            key = f"{bet_id}_{value['value']}"
                            if key not in odds_data or float(value["odd"]) > float(odds_data[key]):
                                odds_data[key] = float(value["odd"])
        return odds_data
    except:
        return {}

def calculate_double_chance_probs(models, features):
    probs_1x2 = models['1x2'].predict_proba(features)[0]
    return {
        '1X': probs_1x2[0] + probs_1x2[1],
        'X2': probs_1x2[2] + probs_1x2[1],
        '12': probs_1x2[0] + probs_1x2[2]
    }

def calculate_over05_ht_prob(prob_over25):
    base_prob = 0.70
    correlation_factor = prob_over25 * 0.3
    prob_method1 = min(base_prob + correlation_factor, 0.90)
    expected_goals_ht = 2.7 * 0.42
    prob_method2 = 1 - poisson.pmf(0, expected_goals_ht)
    return (prob_method1 * 0.4) + (prob_method2 * 0.6)

def get_team_stats(team_name, team_db):
    default_stats = {
        'Last_Form_Pts': 7,
        'Last_Goals_Scored_Avg': 1.4,
        'Last_Goals_Conceded_Avg': 1.4,
        'Last_Over25_Rate': 0.50,
        'Last_BTTS_Rate': 0.50
    }
    
    if team_name in team_db:
        stats = team_db[team_name]
        for key, value in default_stats.items():
            if key not in stats:
                stats[key] = value
        return stats
    
    team_lower = team_name.lower()
    for db_team, stats in team_db.items():
        if db_team.lower() == team_lower:
            for key, value in default_stats.items():
                if key not in stats:
                    stats[key] = value
            return stats
    
    return default_stats.copy()

# ==========================================
# ANÁLISIS MULTI-MERCADO
# ==========================================
def analyze_multi_market(models, fixtures_data, team_db, min_odd=1.8, max_odd=2.8, only_today=True):
    value_bets = []
    now = datetime.now(timezone.utc)
    stats = {'total': 0, 'api_football': 0, 'calculated': 0, 'today': 0}
    
    for event in fixtures_data:
        league = event.get("sport_title", "Unknown")
        home_team = event["home_team"]
        away_team = event["away_team"]
        commence_time = event["commence_time"]
        
        if commence_time.endswith('Z'):
            match_time = datetime.fromisoformat(commence_time.replace('Z', '+00:00'))
        else:
            match_time = datetime.fromisoformat(commence_time)
        
        match_time_es = match_time.astimezone(ZoneInfo("Europe/Madrid"))
        
        if only_today:
            if match_time_es.date() != now.date():
                continue
            stats['today'] += 1
        else:
            time_diff = (match_time_es - now).total_seconds() / 86400
            if time_diff > 3 or time_diff < 0:
                continue
        
        stats['total'] += 1
        
        home_stats = get_team_stats(home_team, team_db)
        away_stats = get_team_stats(away_team, team_db)
        
        form_diff = home_stats.get('Last_Form_Pts', 7) - away_stats.get('Last_Form_Pts', 7)
        goal_threat_diff = home_stats.get('Last_Goals_Scored_Avg', 1.4) - away_stats.get('Last_Goals_Conceded_Avg', 1.4)
        combined_o25 = (home_stats.get('Last_Over25_Rate', 0.50) + away_stats.get('Last_Over25_Rate', 0.50)) / 2
        combined_btts = (home_stats.get('Last_BTTS_Rate', 0.50) + away_stats.get('Last_BTTS_Rate', 0.50)) / 2
        
        features = pd.DataFrame([{
            'Home_Form_Pts': home_stats.get('Last_Form_Pts', 7),
            'Away_Form_Pts': away_stats.get('Last_Form_Pts', 7),
            'Form_Diff': form_diff,
            'Home_Goals_Scored': home_stats.get('Last_Goals_Scored_Avg', 1.4),
            'Away_Goals_Conceded': away_stats.get('Last_Goals_Conceded_Avg', 1.4),
            'Goal_Threat_Diff': goal_threat_diff,
            'Combined_Over25_Rate': combined_o25,
            'Combined_BTTS_Rate': combined_btts
        }])
        
        probs_1x2 = models['1x2'].predict_proba(features)[0]
        prob_over25 = models['over25'].predict_proba(features)[0][1]
        prob_btts = models['btts'].predict_proba(features)[0][1]
        dc_probs = calculate_double_chance_probs(models, features)
        prob_over05_ht = calculate_over05_ht_prob(prob_over25)
        
        fixture_id = get_fixture_id_from_api_football(home_team, away_team, commence_time)
        api_football_odds = get_api_football_odds(fixture_id)
        
        best_odds = {}
        
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                market_key = market["key"]
                for outcome in market.get("outcomes", []):
                    odd = outcome["price"]
                    name = outcome["name"]
                    point = outcome.get("point", 2.5)
                    
                    if odd < min_odd or odd > max_odd:
                        continue
                    
                    key = None
                    if market_key == "h2h":
                        if name == home_team:
                            key = f"1X2_{home_team}"
                        elif name == "Draw":
                            key = "1X2_Draw"
                        elif name == away_team:
                            key = f"1X2_{away_team}"
                    elif market_key == "totals" and name == "Over":
                        if point in [1.5, 2.5, 3.5]:
                            key = f"Over_{point}"
                    
                    if key is not None:
                        if key not in best_odds or odd > best_odds[key]:
                            best_odds[key] = odd
        
        dc_1X_api = api_football_odds.get('12_1X')
        dc_X2_api = api_football_odds.get('12_X2')
        dc_12_api = api_football_odds.get('12_12')
        
        if dc_1X_api and min_odd <= dc_1X_api <= max_odd:
            best_odds['DC_1X'] = dc_1X_api
            stats['api_football'] += 1
        elif min_odd <= (1/dc_probs['1X']) <= max_odd:
            best_odds['DC_1X_CALC'] = 1/dc_probs['1X']
            stats['calculated'] += 1
        
        if dc_X2_api and min_odd <= dc_X2_api <= max_odd:
            best_odds['DC_X2'] = dc_X2_api
            stats['api_football'] += 1
        elif min_odd <= (1/dc_probs['X2']) <= max_odd:
            best_odds['DC_X2_CALC'] = 1/dc_probs['X2']
            stats['calculated'] += 1
        
        if dc_12_api and min_odd <= dc_12_api <= max_odd:
            best_odds['DC_12'] = dc_12_api
            stats['api_football'] += 1
        elif min_odd <= (1/dc_probs['12']) <= max_odd:
            best_odds['DC_12_CALC'] = 1/dc_probs['12']
            stats['calculated'] += 1
        
        over05_ht_api = None
        for api_key, odd in api_football_odds.items():
            if api_key.startswith('6_Over') and '0.5' in api_key:
                over05_ht_api = odd
                break
        
        if over05_ht_api and min_odd <= over05_ht_api <= max_odd:
            best_odds['HT_Over_0.5'] = over05_ht_api
            stats['api_football'] += 1
        elif min_odd <= (1/prob_over05_ht) <= max_odd:
            best_odds['HT_Over_0.5_CALC'] = 1/prob_over05_ht
            stats['calculated'] += 1

        btts_yes_api = api_football_odds.get('8_Yes')
        btts_no_api = api_football_odds.get('8_No')
        
        if btts_yes_api and min_odd <= btts_yes_api <= max_odd:
            best_odds['BTTS_Yes'] = btts_yes_api
            stats['api_football'] += 1
        elif min_odd <= (1/prob_btts) <= max_odd:
            best_odds['BTTS_Yes_CALC'] = 1/prob_btts
            stats['calculated'] += 1
        
        if btts_no_api and min_odd <= btts_no_api <= max_odd:
            best_odds['BTTS_No'] = btts_no_api
            stats['api_football'] += 1
        elif min_odd <= (1/(1-prob_btts)) <= max_odd:
            best_odds['BTTS_No_CALC'] = 1/(1-prob_btts)
            stats['calculated'] += 1
        
        for market_key, odd in best_odds.items():
            prob = None
            mercado_name = None
            is_calculated = '_CALC' in market_key
            
            if market_key.startswith("1X2_"):
                team_part = market_key.split("_")[1]
                if team_part == home_team:
                    prob, mercado_name = probs_1x2[0], f"1X2 - {home_team}"
                elif team_part == "Draw":
                    prob, mercado_name = probs_1x2[1], "1X2 - Empate"
                elif team_part == away_team:
                    prob, mercado_name = probs_1x2[2], f"1X2 - {away_team}"
            elif market_key.startswith("Over_"):
                point = float(market_key.split("_")[1])
                if point == 1.5:
                    prob = models['over15'].predict_proba(features)[0][1]
                    mercado_name = "Over 1.5 Goles"
                elif point == 2.5:
                    prob = models['over25'].predict_proba(features)[0][1]
                    mercado_name = "Over 2.5 Goles"
                elif point == 3.5:
                    prob = models['over35'].predict_proba(features)[0][1]
                    mercado_name = "Over 3.5 Goles"
            elif market_key.startswith("DC_"):
                dc_type = market_key.replace("DC_", "").replace("_CALC", "")
                if dc_type == "1X":
                    prob = dc_probs['1X']
                    mercado_name = "Doble Oportunidad - 1X"
                elif dc_type == "X2":
                    prob = dc_probs['X2']
                    mercado_name = "Doble Oportunidad - X2"
                elif dc_type == "12":
                    prob = dc_probs['12']
                    mercado_name = "Doble Oportunidad - 12"
            elif market_key.startswith("HT_Over_0.5"):
                prob = prob_over05_ht
                mercado_name = "Over 0.5 Goles 1ª Parte"
            elif market_key.startswith("BTTS_"):
                btts_type = market_key.split("_")[1]
                if btts_type == "Yes":
                    prob, mercado_name = prob_btts, "BTTS - Sí (Ambos marcan)"
                elif btts_type == "No":
                    prob, mercado_name = 1 - prob_btts, "BTTS - No"
            
            if prob is None:
                continue
            
            ev = (prob * odd) - 1
            ev_percentage = ev * 100
            
            if ev_percentage > 2.0:
                value_bets.append({
                    "Liga": league,
                    "Partido": f"{home_team} vs {away_team}",
                    "Hora": match_time_es.strftime('%d/%m %H:%M'),
                    "Mercado": mercado_name,
                    "Cuota": odd,
                    "Prob. IA": prob,
                    "Prob. Casa": 1/odd,
                    "EV (%)": ev_percentage,
                    "Fuente": "Cálculo" if is_calculated else "API-Football"
                })
    
    if value_bets:
        df = pd.DataFrame(value_bets)
        df = df.drop_duplicates()
        return df, stats
    return pd.DataFrame(), stats

# ==========================================
# INTERFAZ
# ==========================================
st.title("🌍 Multi-Mercado Value Bet Scanner")
st.markdown("Escaneando **6 mercados** (4 reales + 2 inferidos) en **50+ ligas**")

with st.expander("❓ ¿Qué es el Expected Value (EV)? - Guía completa", expanded=False):
    st.markdown("""
    ## 🎯 ¿Qué es el Expected Value (EV)?
    El **Expected Value (Valor Esperado)** representa el beneficio o pérdida promedio si repitieses la misma apuesta muchas veces.
    
    ### 📐 Fórmula:
    ```
    EV = (Probabilidad_Real × Cuota) - 1
    ```
    
    ### 📊 Interpretación:
    | EV | Significado | Acción |
    |----|-------------|--------|
    | **EV > 10%** | 🟢 Valor EXCEPCIONAL | Apuesta prioritaria |
    | **EV 5-10%** | 🟡 Valor MUY BUENO | Apuesta recomendada |
    | **EV 2-5%** | ⚪ Valor MODERADO | Apuesta opcional |
    | **EV < 2%** | 🔴 Sin valor | NO apostar |
    """)

st.sidebar.header("⚙️ Configuración")
ev_threshold = st.sidebar.slider("Umbral mínimo de EV (%)", min_value=2.0, max_value=20.0, value=5.0, step=1.0)

st.sidebar.markdown("---")
st.sidebar.subheader("📊 Filtro de Cuotas")
col_odd1, col_odd2 = st.sidebar.columns(2)
min_odd = col_odd1.number_input("Cuota mínima", min_value=1.01, max_value=10.0, value=1.8, step=0.1)
max_odd = col_odd2.number_input("Cuota máxima", min_value=1.01, max_value=20.0, value=2.8, step=0.1)

st.sidebar.markdown("---")
st.sidebar.subheader("📅 Filtro de Fecha")
only_today = st.sidebar.checkbox("Solo partidos de HOY", value=True)

team_db_preview = load_team_database()
st.sidebar.markdown("---")
st.sidebar.info(f"📁 Equipos en base de datos: **{len(team_db_preview)}**")

st.sidebar.markdown("### 🎯 Mercados Activos:")
st.sidebar.markdown("- ✅ Over 1.5, 2.5, 3.5 Goles")
st.sidebar.markdown("- ✅ 1X2 (Ganador)")
st.sidebar.markdown("- ✅ BTTS (Ambos marcan)")
st.sidebar.markdown("- ✅ Doble Oportunidad (1X, X2, 12)")
st.sidebar.markdown("- ✅ Over 0.5 1ª Parte")

st.sidebar.markdown("---")
st.sidebar.subheader("🔑 Estado de API Keys")
st.sidebar.info(f"Keys configuradas y activas: {len(THE_ODDS_API_KEYS)}/3")
if len(THE_ODDS_API_KEYS) == 0:
    st.sidebar.error("⚠️ ¡Añade THE_ODDS_API_KEY_1 en los Secrets!")

models = load_all_models()
team_db = load_team_database()

if not models:
    st.stop()

if st.button("🔄 Escanear Todos los Mercados Ahora"):
    with st.spinner("Analizando mercados..."):
        fixtures = scan_all_markets()
        if fixtures:
            df_bets, stats = analyze_multi_market(models, fixtures, team_db, min_odd=min_odd, max_odd=max_odd, only_today=only_today)
            
            tracker_register = StatsTracker()
            registered = 0
            if not df_bets.empty:
                for _, row in df_bets.iterrows():
                    if tracker_register.register_pick(row.to_dict()):
                        registered += 1
            
            st.session_state['df_bets'] = df_bets
            st.session_state['stats'] = stats
            st.session_state['total_fixtures'] = len(fixtures)
            st.session_state['registered_picks'] = registered
            st.success(f"✅ Escaneo completado | 💾 {registered} picks registrados")
        else:
            st.error("No se pudieron obtener datos de la API")

if 'df_bets' in st.session_state:
    df_bets = st.session_state['df_bets']
    stats = st.session_state.get('stats', {})
    total_fixtures = st.session_state.get('total_fixtures', 0)
    df_filtered = df_bets[df_bets['EV (%)'] >= ev_threshold].copy()
    
    st.markdown("---")
    st.subheader("🔧 Filtros y Ordenación")
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    
    with col_f1:
        selected_market = st.selectbox("Filtrar por Mercado:", options=["Todos"] + sorted(df_filtered["Mercado"].unique().tolist()) if not df_filtered.empty else ["Todos"])
    with col_f2:
        selected_league = st.selectbox("🏆 Filtrar por Liga:", options=["Todas"] + sorted(df_filtered["Liga"].unique().tolist()) if not df_filtered.empty else ["Todas"])
    with col_f3:
        selected_source = st.selectbox("🔖 Filtrar por Fuente:", options=["Todas", "API-Football", "Cálculo"])
    with col_f4:
        sort_by = st.selectbox("📈 Ordenar por:", options=["EV (%) ↓", "EV (%) ↑", "Cuota ↓", "Cuota ↑", "Prob. IA ↓", "Hora ↑", "Liga A-Z"])
    
    if selected_market != "Todos" and not df_filtered.empty:
        df_filtered = df_filtered[df_filtered["Mercado"] == selected_market]
    if selected_league != "Todas" and not df_filtered.empty:
        df_filtered = df_filtered[df_filtered["Liga"] == selected_league]
    if selected_source != "Todas" and not df_filtered.empty:
        df_filtered = df_filtered[df_filtered["Fuente"] == selected_source]
    
    if not df_filtered.empty:
        sort_col = sort_by.replace(" ↓", "").replace(" ↑", "").replace(" A-Z", "")
        ascending = "↑" in sort_by or "A-Z" in sort_by
        df_filtered = df_filtered.sort_values(by=sort_col, ascending=ascending)
    
    st.markdown("---")
    st.subheader("📊 Resumen Ejecutivo")
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Partidos Analizados", total_fixtures)
    kpi2.metric("Value Bets Detectadas", len(df_filtered))
    if not df_filtered.empty:
        kpi3.metric("Mejor EV", f"{df_filtered['EV (%)'].max():.1f}%")
        kpi4.metric("EV Promedio", f"{df_filtered['EV (%)'].mean():.1f}%")
    else:
        kpi3.metric("Mejor EV", "N/A")
        kpi4.metric("EV Promedio", "N/A")
    
    if not df_filtered.empty:
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.markdown("#### 📊 Distribución por Mercado")
            for market, count in df_filtered["Mercado"].value_counts().items():
                st.markdown(f"- **{market}**: {count}")
        with col_b:
            st.markdown("#### 🏆 Top 3 Ligas")
            for league, count in df_filtered["Liga"].value_counts().head(3).items():
                st.markdown(f"- **{league}**: {count}")
        with col_c:
            st.markdown("#### 🔖 Distribución por Fuente")
            for source, count in df_filtered["Fuente"].value_counts().items():
                st.markdown(f"- **{source}**: {count}")
        
        st.markdown("---")
        st.markdown("#### 💡 Recomendación del Sistema")
        high_ev = df_filtered[df_filtered['EV (%)'] >= 10]
        medium_ev = df_filtered[(df_filtered['EV (%)'] >= 5) & (df_filtered['EV (%)'] < 10)]
        low_ev = df_filtered[(df_filtered['EV (%)'] >= 2) & (df_filtered['EV (%)'] < 5)]
        
        if len(high_ev) > 0:
            st.success(f"🟢 **{len(high_ev)} apuestas de ALTO valor** (EV ≥ 10%). Prioridad máxima.")
        if len(medium_ev) > 0:
            st.info(f"🟡 **{len(medium_ev)} apuestas de BUEN valor** (EV 5-10%). Recomendadas.")
        if len(low_ev) > 0:
            st.warning(f"⚪ **{len(low_ev)} apuestas de valor moderado** (EV 2-5%). Opcionales.")
        
        avg_odd = df_filtered['Cuota'].mean()
        if avg_odd < 1.8:
            st.markdown(f"📉 **Perfil de riesgo**: Conservador (cuota media {avg_odd:.2f})")
        elif avg_odd < 2.5:
            st.markdown(f"📊 **Perfil de riesgo**: Moderado (cuota media {avg_odd:.2f})")
        else:
            st.markdown(f"📈 **Perfil de riesgo**: Agresivo (cuota media {avg_odd:.2f})")
    else:
        st.info(f"💡 No se encontraron Value Bets con EV > {ev_threshold}% con los filtros actuales.")
    
    st.markdown("---")
    st.subheader(f"🎯 Oportunidades Detectadas ({len(df_filtered)} encontradas)")
    
    if not df_filtered.empty:
        df_display = df_filtered.head(50).copy()
        df_display["Prob. IA"] = df_display["Prob. IA"].apply(lambda x: f"{x:.1%}")
        df_display["Prob. Casa"] = df_display["Prob. Casa"].apply(lambda x: f"{x:.1%}")
        df_display["EV (%)"] = df_display["EV (%)"].apply(lambda x: f"{x:.1f}%")
        
        def color_ev(val):
            try:
                ev_val = float(val.replace('%', ''))
                if ev_val > 10: return 'background-color: #2ecc71; color: white; font-weight: bold'
                elif ev_val > 5: return 'background-color: #27ae60; color: white'
                elif ev_val > 2: return 'background-color: #95e1d3; color: black'
                else: return 'background-color: #f9e79f; color: black'
            except: return ''
        
        st.dataframe(df_display.style.map(color_ev, subset=["EV (%)"]), use_container_width=True, hide_index=True)
        
        if len(df_filtered) > 50:
            st.info(f"Mostrando las 50 mejores de {len(df_filtered)} Value Bets.")
    else:
        st.info("No hay resultados para mostrar con los filtros actuales.")

# ==========================================
# ESTADÍSTICAS HISTÓRICAS
# ==========================================
st.markdown("---")
st.subheader("📈 Estadísticas Históricas")
st.caption("Rendimiento acumulado de todos los picks registrados por el bot")

tracker = StatsTracker()
hist_stats = tracker.calculate_stats()

if hist_stats['settled'] > 0:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Picks", hist_stats['total'])
    col2.metric("Liquidados", hist_stats['settled'])
    col3.metric("Pendientes", hist_stats['pending'])
    col4.metric("✅ Aciertos", hist_stats['wins'])
    col5.metric("❌ Errores", hist_stats['losses'])
    
    col6, col7, col8, col9 = st.columns(4)
    col6.metric("📊 Hit Rate", f"{hist_stats['hit_rate']:.1f}%")
    col7.metric("💰 PnL", f"{hist_stats['pnl']:+.2f} u")
    col8.metric("📈 Yield", f"{hist_stats['yield']:+.1f}%")
    if hist_stats['avg_ev_declared'] is not None:
        col9.metric("📉 EV medio (declarado)", f"{hist_stats['avg_ev_declared']:+.1f}%")
    else:
        col9.metric("📉 EV medio", "N/A")
    
    if hist_stats['calibration_gap'] is not None:
        gap = hist_stats['calibration_gap']
        if gap > 5:
            st.warning(f"⚠️ El modelo SOBREESTIMA el valor en **{gap:+.1f} pp**. Los picks prometen más de lo que entregan.")
        elif gap < -5:
            st.success(f"✅ El modelo es CONSERVADOR en **{abs(gap):.1f} pp**. Mejor de lo esperado.")
        else:
            st.info(f"✅ Calibración correcta (gap: {gap:+.1f} pp)")
    
    st.markdown("#### 📊 Rendimiento por Mercado")
    market_stats = tracker.get_stats_by_market()
    if market_stats:
        df_markets = pd.DataFrame(market_stats).T
        df_markets = df_markets.sort_values('Total', ascending=False)
        st.dataframe(df_markets, use_container_width=True)
    
    st.markdown("#### 🏆 Rendimiento por Liga")
    league_stats = tracker.get_stats_by_league()
    if league_stats:
        df_leagues = pd.DataFrame(league_stats).T
        df_leagues = df_leagues.sort_values('Total', ascending=False).head(15)
        st.dataframe(df_leagues, use_container_width=True)

elif hist_stats['total'] > 0:
    st.info(f"📊 Hay **{hist_stats['total']} picks registrados** pero ninguno liquidado aún.")
    st.info("💡 Los picks se liquidan automáticamente con los cron de liquidación (07:15 y 13:15 UTC).")
else:
    st.info("📊 Aún no hay picks registrados. Los picks se guardarán a partir del próximo escaneo.")

# ==========================================
# GRÁFICOS DE EVOLUCIÓN (v0.4-C)
# ==========================================
st.markdown("---")
st.subheader("📉 Gráficos de Evolución")
st.caption("Evolución temporal del rendimiento REAL (picks liquidados)")

with st.expander("❓ Cómo leer estos gráficos - Guía rápida", expanded=False):
    st.markdown("""
    ### 💰 PnL acumulado (unidades, stake=1)
    - Cada punto es un pick liquidado, en orden de liquidación.
    - **Línea verde subiendo** = beneficio sostenido; **bajando** = racha negativa.
    - La **línea gris discontinua en 0** es el punto de equilibrio: por encima ganas, por debajo pierdes.
    - La **pendiente** de la curva es tu yield real en el tiempo.

    ### 🎯 Hit rate rodante (ventana de 10 picks)
    - Muestra el % de aciertos de **los últimos 10 picks** en cada punto.
    - La **línea verde punteada** es tu hit rate global (media de todos los liquidados).
    - Picos y valles son rachas cortas: un valle puntual no es grave si la línea global se mantiene.

    ### ⚖️ Calibración: prometido vs real
    - **Naranja** = Prob. IA media prometida hasta ese momento.
    - **Verde** = % de aciertos real hasta ese momento.
    - Si la naranja se separa **por encima** de la verde: la IA promete más de lo que cumple → exige más EV o baja stakes.
    - Si van **pegadas** (±10 pp): sistema bien calibrado, confía en los EV declarados.
    - Si la verde va **por encima**: la IA es conservadora; el sistema rinde más de lo que anuncia.

    ### 🔻 CLV medio acumulado
    - **CLV** = (cuota tomada / cuota de cierre − 1). Mide si bates al mercado.
    - **Línea morada por encima de 0** = encuentras valor antes que la casa → señal de rentabilidad a largo plazo, incluso si el PnL corto es negativo.
    - **Línea morada por debajo de 0** = tus cuotas no baten al cierre → el "valor" puede ser ruido del modelo.
    """)

picks_hist = tracker.get_all_picks()
settled_hist = sorted(
    [p for p in picks_hist if p['status'] in ('won', 'lost')],
    key=lambda p: p.get('settled_at') or p.get('timestamp') or ''
)

if len(settled_hist) < 2:
    st.info("📈 Los gráficos aparecerán en cuanto haya al menos 2 picks liquidados.")
else:
    xs = list(range(1, len(settled_hist) + 1))
    cum_pnl, acc = [], 0.0
    flags = []
    cum_pia, cum_hit = [], []
    sum_pia, wins_acc = 0.0, 0
    
    for i, p in enumerate(settled_hist):
        acc += (p['cuota'] - 1) if p['status'] == 'won' else -1.0
        cum_pnl.append(round(acc, 2))
        w = 1 if p['status'] == 'won' else 0
        wins_acc += w
        flags.append(w)
        if p['prob_ia'] is not None:
            sum_pia += p['prob_ia']
        cum_pia.append((sum_pia / (i + 1)) * 100)
        cum_hit.append((wins_acc / (i + 1)) * 100)
    
    W = 10
    rolling_hit = [
        sum(flags[max(0, i - W + 1): i + 1]) / len(flags[max(0, i - W + 1): i + 1]) * 100
        for i in range(len(flags))
    ]
    overall_hit = sum(flags) / len(flags) * 100
    
    # Gráfico 1: PnL acumulado
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=xs, y=cum_pnl, mode='lines+markers',
                              name='PnL acumulado',
                              line=dict(color='#2ecc71', width=2.5)))
    fig1.add_hline(y=0, line_dash='dash', line_color='gray')
    fig1.update_layout(title='💰 PnL acumulado (unidades, stake=1)',
                       xaxis_title='Pick liquidado nº', yaxis_title='Unidades',
                       margin=dict(t=50, b=40))
    st.plotly_chart(fig1, use_container_width=True)
    
    col_g1, col_g2 = st.columns(2)
    with col_g1:
        # Gráfico 2: Hit rate rodante
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=xs, y=rolling_hit, mode='lines',
                                  name='Hit rate rodante (10)',
                                  line=dict(color='#3498db', width=2)))
        fig2.add_hline(y=overall_hit, line_dash='dot', line_color='green')
        fig2.update_layout(title=f'🎯 Hit rate rodante (global: {overall_hit:.1f}%)',
                           xaxis_title='Pick liquidado nº', yaxis_title='%',
                           margin=dict(t=50, b=40))
        st.plotly_chart(fig2, use_container_width=True)
    with col_g2:
        # Gráfico 3: Calibración acumulada
        fig3 = go.Figure()
        fig3.add_trace(go.Scatter(x=xs, y=cum_pia, mode='lines',
                                  name='Prob. IA prometida',
                                  line=dict(color='#e67e22', width=2)))
        fig3.add_trace(go.Scatter(x=xs, y=cum_hit, mode='lines',
                                  name='Hit rate real',
                                  line=dict(color='#27ae60', width=2)))
        fig3.update_layout(title='⚖️ Calibración: prometido vs real',
                           xaxis_title='Pick liquidado nº', yaxis_title='%',
                           margin=dict(t=50, b=40))
        st.plotly_chart(fig3, use_container_width=True)

# ==========================================
# CLV (v0.4-D Parte 2)
# ==========================================
st.markdown("---")
st.subheader("🔻 Closing Line Value (CLV)")
st.caption("¿Bates al mercado? CLV = (cuota tomada / cuota de cierre − 1). Positivo = encuentras valor antes que la casa.")

clv_picks = [p for p in picks_hist if p.get('closing_odds') and p.get('cuota')]

if not clv_picks:
    st.info("🔻 El CLV aparecerá en cuanto los cron de Closing Odds (08/14/20 UTC) capturen cuotas de cierre.")
else:
    clv_sorted = sorted(
        clv_picks,
        key=lambda p: p.get('captured_closing_at') or p.get('timestamp') or ''
    )
    clvs = [((p['cuota'] / p['closing_odds']) - 1) * 100 for p in clv_sorted]
    avg_clv = sum(clvs) / len(clvs)
    beat = sum(1 for c in clvs if c > 0)
    beat_pct = beat / len(clvs) * 100
    
    cum_clv = []
    acc = 0.0
    for i, c in enumerate(clvs):
        acc += c
        cum_clv.append(acc / (i + 1))
    
    c1, c2, c3 = st.columns(3)
    c1.metric("🔻 CLV medio", f"{avg_clv:+.1f}%")
    c2.metric("🎯 Bate al cierre", f"{beat_pct:.0f}% ({beat}/{len(clvs)})")
    c3.metric("📊 Picks con cierre", len(clv_picks))
    
    if avg_clv > 0:
        st.success("✅ CLV positivo: estás encontrando valor antes que el mercado. Señal de rentabilidad a largo plazo.")
    else:
        st.warning("⚠️ CLV negativo o nulo: tus cuotas no baten al cierre. Revisa umbrales de EV.")
    
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(x=list(range(1, len(cum_clv) + 1)), y=cum_clv,
                              mode='lines+markers', name='CLV medio acumulado',
                              line=dict(color='#9b59b6', width=2.5)))
    fig4.add_hline(y=0, line_dash='dash', line_color='gray')
    fig4.update_layout(title='🔻 CLV medio acumulado (%)',
                       xaxis_title='Pick con cierre nº', yaxis_title='%',
                       margin=dict(t=50, b=40))
    st.plotly_chart(fig4, use_container_width=True)

# ==========================================
# FOOTER
# ==========================================
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #888; font-size: 0.9em;'>
    <p>⚠️ <strong>Aviso:</strong> Herramienta de análisis estadístico. Las apuestas conllevan riesgo. Apuesta con responsabilidad.</p>
    <p>🧠 Powered by XGBoost + API-Football + The Odds API</p>
</div>
""", unsafe_allow_html=True)

# ==========================================
# EXPORTAR DATOS PARA EL BOT DE TELEGRAM
# ==========================================
st.markdown("---")
st.subheader("🤖 Exportar Estadísticas a GitHub")
st.caption("Genera un archivo `datos.json` en tu repositorio para que el bot de Telegram lo lea automáticamente.")

if st.button("📤 Exportar Datos Ahora"):
    try:
        from github import Github
        import json
        from datetime import datetime
        
        repo_name = os.getenv("GITHUB_REPO_NAME")
        github_token = os.getenv("GITHUB_TOKEN")
        
        if not github_token or not repo_name:
            st.error("❌ Falta configurar GITHUB_TOKEN o GITHUB_REPO_NAME en los Secrets de Streamlit.")
        else:
            with st.spinner("Conectando con GitHub y exportando datos..."):
                g = Github(github_token)
                repo = g.get_repo(repo_name)
                
                # Recopilar métricas con verificación de existencia segura
                export_data = {
                    "total_picks": hist_stats.get('total', 0) if 'hist_stats' in locals() else 0,
                    "liquidados": hist_stats.get('settled', 0) if 'hist_stats' in locals() else 0,
                    "pendientes": hist_stats.get('pending', 0) if 'hist_stats' in locals() else 0,
                    "aciertos": hist_stats.get('wins', 0) if 'hist_stats' in locals() else 0,
                    "errores": hist_stats.get('losses', 0) if 'hist_stats' in locals() else 0,
                    "hit_rate": round(hist_stats.get('hit_rate', 0), 1) if 'hist_stats' in locals() else 0.0,
                    "pnl": round(hist_stats.get('pnl', 0), 2) if 'hist_stats' in locals() else 0.0,
                    "yield": round(hist_stats.get('yield', 0), 1) if 'hist_stats' in locals() else 0.0,
                    "ev_medio": round(hist_stats.get('avg_ev_declared', 0), 1) if 'hist_stats' in locals() and hist_stats.get('avg_ev_declared') is not None else 0.0,
                    "sobreestimacion": round(hist_stats.get('calibration_gap', 0), 1) if 'hist_stats' in locals() and hist_stats.get('calibration_gap') is not None else 0.0,
                    "clv_medio": round(locals().get('avg_clv', 0.0), 1),
                    "bate_cierre_pct": round(locals().get('beat_pct', 0.0), 1),
                    "ultima_actualizacion": datetime.now().strftime("%d/%m/%Y %H:%M")
                }
                
                # Añadir mejor mercado si existe
                if 'market_stats' in locals() and market_stats:
                    try:
                        df_m = pd.DataFrame(market_stats).T
                        if not df_m.empty and 'Yield' in df_m.columns:
                            best_market = df_m['Yield'].idxmax()
                            export_data["mejor_mercado"] = str(best_market)
                            export_data["yield_mejor_mercado"] = round(float(df_m.loc[best_market, 'Yield']), 1)
                    except Exception as e:
                        st.warning(f"⚠️ No se pudo calcular el mejor mercado: {e}")

                content = json.dumps(export_data, indent=4, ensure_ascii=False)
                
                # Crear o actualizar el archivo en GitHub
                try:
                    contents = repo.get_contents("datos.json")
                    repo.update_file("datos.json", "🤖 Auto-actualización stats para Telegram", content, contents.sha)
                except:
                    repo.create_file("datos.json", "🤖 Creación inicial stats para Telegram", content)
                    
                st.success(f"✅ ¡Datos exportados correctamente a {repo_name}/datos.json!")
                with st.expander("Ver datos exportados (JSON)"):
                    st.json(export_data)
                
    except ImportError:
        st.error("❌ Falta la librería `PyGithub`. Añádela a tu requirements.txt de Streamlit y espera a que se reinicie la app.")
    except Exception as e:
        st.error(f"❌ Error al exportar: {e}")
