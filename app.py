import streamlit as st
import pandas as pd
import numpy as np
import requests
import joblib
import warnings
from datetime import datetime, timezone
from scipy.stats import poisson
warnings.filterwarnings('ignore')

st.set_page_config(page_title="⚽ Multi-Mercado Value Bet Scanner", layout="wide")

# ==========================================
# CONFIGURACIÓN - LEER DESDE SECRETS DE STREAMLIT
# ==========================================
import os

THE_ODDS_API_KEY = st.secrets.get("THE_ODDS_API_KEY", os.getenv("THE_ODDS_API_KEY", ""))
API_FOOTBALL_KEY = st.secrets.get("API_FOOTBALL_KEY", os.getenv("API_FOOTBALL_KEY", "00599a23daf70c08d47f1db56dfe5eb5"))
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
@st.cache_resource(ttl=1800, show_spinner="Escaneando mercados...")
def scan_all_markets():
    url = f"https://api.the-odds-api.com/v4/sports/soccer/odds"
    params = {
        "regions": "eu,us",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
        "apiKey": THE_ODDS_API_KEY
    }
    
    response = requests.get(url, params=params, timeout=15)
    if response.status_code != 200:
        st.error(f"Error en The Odds API: {response.text}")
        return []
    
    return response.json()

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
def analyze_multi_market(models, fixtures_data, team_db, min_odd=1.3, max_odd=3.0, only_today=True):
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
        
        if only_today:
            if match_time.date() != now.date():
                continue
            stats['today'] += 1
        else:
            time_diff = (match_time - now).total_seconds() / 86400
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
        
        # Doble Oportunidad
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
        
        # Over 0.5 Primera Parte
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
        
        # Calcular EV
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
            
            if prob is None:
                continue
            
            ev = (prob * odd) - 1
            ev_percentage = ev * 100
            
            if ev_percentage > 2.0:
                value_bets.append({
                    "Liga": league,
                    "Partido": f"{home_team} vs {away_team}",
                    "Hora": commence_time[:16].replace('T', ' '),
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

# LEYENDA EV
with st.expander("❓ ¿Qué es el Expected Value (EV)? - Guía completa", expanded=False):
    st.markdown("""
    ## 🎯 ¿Qué es el Expected Value (EV)?
    
    El **Expected Value (Valor Esperado)** es el concepto matemático más importante en apuestas deportivas profesionales. 
    Representa el **beneficio o pérdida promedio** que esperarías obtener si repitieses la misma apuesta muchas veces.
    
    ### 📐 Fórmula:
    ```
    EV = (Probabilidad_Real × Cuota) - 1
    ```
    
    ### 📊 Interpretación:
    | EV | Significado | Acción |
    |----|-------------|--------|
    | **EV > 10%** | 🟢 Valor EXCEPCIONAL | Apuesta prioritaria |
    | **EV 5-10%** | 🟡 Valor MUY BUENO | Apuesta recomendada |
    | **EV 2-5%** |  Valor MODERADO | Apuesta opcional |
    | **EV < 2%** | ⚪ Sin valor | NO apostar |
    | **EV negativo** | 🔴 Pérdida esperada | EVITAR |
    
    ### 💡 Ejemplo práctico:
    - **Partido**: Racing Club vs Tigre BA
    - **Mercado**: Over 2.5 Goles
    - **Cuota**: 2.75
    - **Probabilidad IA**: 53.2%
    - **Cálculo**: (0.532 × 2.75) - 1 = **+0.463 = +46.3% EV**
    
    ### ⚠️ Importante:
    - Un EV positivo **NO garantiza ganar** cada apuesta individual
    - El valor está en la **repetición a largo plazo**
    - Gestiona tu banca: nunca arriesgues más del 5% por apuesta
    """)

# SIDEBAR
st.sidebar.header("⚙️ Configuración")
ev_threshold = st.sidebar.slider("Umbral mínimo de EV (%)", min_value=2.0, max_value=20.0, value=5.0, step=1.0)

st.sidebar.markdown("---")
st.sidebar.subheader(" Filtro de Cuotas")
col_odd1, col_odd2 = st.sidebar.columns(2)
min_odd = col_odd1.number_input("Cuota mínima", min_value=1.01, max_value=10.0, value=1.3, step=0.1)
max_odd = col_odd2.number_input("Cuota máxima", min_value=1.01, max_value=20.0, value=3.0, step=0.1)

st.sidebar.markdown("---")
st.sidebar.subheader(" Filtro de Fecha")
only_today = st.sidebar.checkbox("Solo partidos de HOY", value=True, help="Si desmarcas, verás partidos de los próximos 3 días")

team_db_preview = load_team_database()
st.sidebar.markdown("---")
st.sidebar.info(f"📁 Equipos en base de datos: **{len(team_db_preview)}**")

st.sidebar.markdown("### 🎯 Mercados Activos:")
st.sidebar.markdown("- ✅ Over 1.5, 2.5, 3.5 Goles")
st.sidebar.markdown("- ✅ 1X2 (Ganador)")
st.sidebar.markdown("- ✅ Doble Oportunidad (1X, X2, 12)")
st.sidebar.markdown("- ✅ Over 0.5 1ª Parte")

st.sidebar.markdown("### 🔒 Filtros activos:")
st.sidebar.markdown(f"- ✅ Cuotas entre **{min_odd}** y **{max_odd}**")
st.sidebar.markdown(f"- ✅ {'Solo hoy' if only_today else 'Próximos 3 días'}")

# EJECUCIÓN
models = load_all_models()
team_db = load_team_database()

if not models:
    st.stop()

if st.button("🔄 Escanear Todos los Mercados Ahora"):
    with st.spinner("Analizando 6 mercados..."):
        fixtures = scan_all_markets()
        if fixtures:
            df_bets, stats = analyze_multi_market(
                models, fixtures, team_db, 
                min_odd=min_odd, max_odd=max_odd, only_today=only_today
            )
            st.session_state['df_bets'] = df_bets
            st.session_state['stats'] = stats
            st.session_state['total_fixtures'] = len(fixtures)
            st.success(f"✅ Escaneo completado")
        else:
            st.error("No se pudieron obtener datos de la API")

# MOSTRAR RESULTADOS
if 'df_bets' in st.session_state:
    df_bets = st.session_state['df_bets']
    stats = st.session_state.get('stats', {})
    total_fixtures = st.session_state.get('total_fixtures', 0)
    
    df_filtered = df_bets[df_bets['EV (%)'] >= ev_threshold].copy()
    
    st.markdown("---")
    st.subheader("🔧 Filtros y Ordenación")
    
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    
    with col_f1:
        selected_market = st.selectbox(
            " Filtrar por Mercado:",
            options=["Todos"] + sorted(df_filtered["Mercado"].unique().tolist()) if not df_filtered.empty else ["Todos"]
        )
    
    with col_f2:
        selected_league = st.selectbox(
            " Filtrar por Liga:",
            options=["Todas"] + sorted(df_filtered["Liga"].unique().tolist()) if not df_filtered.empty else ["Todas"]
        )
    
    with col_f3:
        selected_source = st.selectbox(
            " Filtrar por Fuente:",
            options=["Todas", "API-Football", "Cálculo"]
        )
    
    with col_f4:
        sort_by = st.selectbox(
            " Ordenar por:",
            options=["EV (%) ↓", "EV (%) ↑", "Cuota ↓", "Cuota ↑", "Prob. IA ↓", "Hora ↑", "Liga A-Z"]
        )
    
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
    
    # RESUMEN EJECUTIVO
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
            st.markdown("####  Distribución por Mercado")
            market_dist = df_filtered["Mercado"].value_counts()
            for market, count in market_dist.items():
                st.markdown(f"- **{market}**: {count}")
        
        with col_b:
            st.markdown("#### 🏆 Top 3 Ligas")
            league_dist = df_filtered["Liga"].value_counts().head(3)
            for league, count in league_dist.items():
                st.markdown(f"- **{league}**: {count}")
        
        with col_c:
            st.markdown("#### 🔖 Distribución por Fuente")
            source_dist = df_filtered["Fuente"].value_counts()
            for source, count in source_dist.items():
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
            st.warning(f" **{len(low_ev)} apuestas de valor moderado** (EV 2-5%). Opcionales.")
        
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
        
        st.dataframe(
            df_display.style.map(color_ev, subset=["EV (%)"]),
            use_container_width=True,
            hide_index=True
        )
        
        if len(df_filtered) > 50:
            st.info(f"Mostrando las 50 mejores de {len(df_filtered)} Value Bets.")
    else:
        st.info("No hay resultados para mostrar con los filtros actuales.")
else:
    st.info("Haz clic en '🔄 Escanear Todos los Mercados Ahora' para comenzar.")

st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #888; font-size: 0.9em;'>
    <p>⚠️ <strong>Aviso:</strong> Herramienta de análisis estadístico. Las apuestas conllevan riesgo. Apuesta con responsabilidad.</p>
    <p> Powered by XGBoost + API-Football + The Odds API</p>
</div>
""", unsafe_allow_html=True)
