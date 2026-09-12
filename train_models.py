#!/usr/bin/env python3
"""
train_models.py · Re-entrenado de los 5 modelos XGBoost con fútbol actual.
- Datos: football-data.co.uk (20 ligas, temporadas 23/24 → 26/27)
- Features IDÉNTICAS a auto_scan.py (ventana 5 y fórmulas de build_team_db.py)
- Split temporal: entrena con 23/24-25/26 y evalúa con 26/27 (escenario real)
- Gates de despliegue:
    · Gate temporal: todos los gaps de la temporada 26/27 dentro de ±10 pp
    · Gate real (con --validate): gap de los modelos nuevos sobre tus picks
      liquidados (features guardadas en Supabase) ≤ ±10 pp y Brier menor
- Si ambos pasan: escribe deploy_ok.txt y el marcador model_deployed_at
  (el workflow hace commit de los .pkl solo si existe deploy_ok.txt)
"""
import io
import bisect
import time
import argparse
import logging
import requests
import joblib
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from xgboost import XGBClassifier

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WINDOW = 5
SEASONS = ['2324', '2425', '2526', '2627']
TEST_SEASON = '2627'
GATE_PP = 10.0

FEATURE_COLS = ['Home_Form_Pts', 'Away_Form_Pts', 'Form_Diff',
                'Home_Goals_Scored', 'Away_Goals_Conceded',
                'Goal_Threat_Diff', 'Combined_Over25_Rate', 'Combined_BTTS_Rate']

DEFAULTS = {'pts': 7.0, 'gf': 1.4, 'ga': 1.4, 'o25': 0.50, 'btts': 0.50}

LEAGUE_CODES = {
    "E0": "England Premier League", "E1": "England Championship",
    "E2": "England League One", "E3": "England League Two",
    "SP1": "Spain La Liga", "SP2": "Spain Segunda",
    "I1": "Italy Serie A", "I2": "Italy Serie B",
    "D1": "Germany Bundesliga", "D2": "Germany Bundesliga 2",
    "F1": "France Ligue 1", "F2": "France Ligue 2",
    "N1": "Netherlands Eredivisie", "P1": "Portugal Primeira Liga",
    "B1": "Belgium First Division A", "T1": "Turkey Super Lig",
    "G1": "Greece Super League", "SC0": "Scotland Premiership",
    "SC1": "Scotland Championship",
}

XGB_PARAMS = dict(n_estimators=250, max_depth=3, learning_rate=0.05,
                  subsample=0.9, colsample_bytree=0.9, min_child_weight=5,
                  reg_lambda=1.0, random_state=42, n_jobs=2)


def download_all():
    dfs = []
    for season in SEASONS:
        for code, name in LEAGUE_CODES.items():
            url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
            try:
                r = requests.get(url, timeout=20)
                if r.status_code != 200 or not r.text.strip():
                    continue
                df = pd.read_csv(io.StringIO(r.text))
                if not {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}.issubset(df.columns):
                    continue
                df = df[["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]].copy()
                df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
                df["FTHG"] = pd.to_numeric(df["FTHG"], errors="coerce")
                df["FTAG"] = pd.to_numeric(df["FTAG"], errors="coerce")
                df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
                df["Season"] = season
                dfs.append(df)
                logger.info(f"✅ {season}/{code} {name}: {len(df)} partidos")
            except Exception as e:
                logger.warning(f"⚠️ {season}/{code}: {e}")
            time.sleep(0.15)
    if not dfs:
        raise SystemExit("❌ Sin datos descargados")
    return pd.concat(dfs, ignore_index=True)


def rolling_stats(dates, stats, d):
    if not dates:
        return dict(DEFAULTS)
    idx = bisect.bisect_left(dates, d)
    win = stats[max(0, idx - WINDOW):idx]
    if not win:
        return dict(DEFAULTS)
    n = len(win)
    return {
        'pts': sum(w[2] for w in win),
        'gf': sum(w[0] for w in win) / n,
        'ga': sum(w[1] for w in win) / n,
        'o25': sum(w[3] for w in win) / n,
        'btts': sum(w[4] for w in win) / n,
    }


def build_dataset(results):
    results = results.sort_values('Date').reset_index(drop=True)
    hist = {}
    feats, labs, seasons = [], [], []
    for r in results.itertuples(index=False):
        d, home, away = r.Date, r.HomeTeam, r.AwayTeam
        hg, ag = int(r.FTHG), int(r.FTAG)
        h = rolling_stats(*hist.get(home, ([], [])), d)
        a = rolling_stats(*hist.get(away, ([], [])), d)
        feats.append([h['pts'], a['pts'], h['pts'] - a['pts'],
                      h['gf'], a['ga'], h['gf'] - a['ga'],
                      (h['o25'] + a['o25']) / 2, (h['btts'] + a['btts']) / 2])
        total = hg + ag
        o25 = 1 if total >= 3 else 0
        btts = 1 if hg > 0 and ag > 0 else 0
        labs.append({
            'y1x2': 0 if hg > ag else (2 if hg < ag else 1),
            'o15': 1 if total >= 2 else 0,
            'o25': o25,
            'o35': 1 if total >= 4 else 0,
            'btts': btts,
        })
        seasons.append(r.Season)
        for team, gf, ga in ((home, hg, ag), (away, ag, hg)):
            td, ts = hist.get(team, ([], []))
            td.append(d)
            ts.append((gf, ga, 3 if gf > ga else (1 if gf == ga else 0), o25, btts))
            hist[team] = (td, ts)
    return pd.DataFrame(feats, columns=FEATURE_COLS), pd.DataFrame(labs), np.array(seasons)


def train_all(X, y):
    models = {}
    m = XGBClassifier(objective='multi:softprob', num_class=3, **XGB_PARAMS)
    m.fit(X, y['y1x2'])
    models['1x2'] = m
    for name, col in [('over15', 'o15'), ('over25', 'o25'),
                      ('over35', 'o35'), ('btts', 'btts')]:
        mb = XGBClassifier(objective='binary:logistic', **XGB_PARAMS)
        mb.fit(X, y[col])
        models[name] = mb
    logger.info("✅ 5 modelos entrenados")
    return models


def evaluate(models, X, y):
    logger.info(f"🧪 EVALUACIÓN TEMPORAL (temporada {TEST_SEASON}, n={len(X)})")
    gaps = {}
    p1 = models['1x2'].predict_proba(X)
    acc = float((p1.argmax(axis=1) == y['y1x2'].values).mean())
    p_true = p1[np.arange(len(X)), y['y1x2'].values]
    gaps['1x2'] = (float(p_true.mean()) - acc) * 100
    logger.info(f"   1X2  · acc={acc*100:.1f}% · confianza={p_true.mean()*100:.1f}% "
                f"· gap={gaps['1x2']:+.1f} pp")
    for name, col in [('over15', 'o15'), ('over25', 'o25'),
                      ('over35', 'o35'), ('btts', 'btts')]:
        p = models[name].predict_proba(X)[:, 1]
        t = y[col].values
        gaps[name] = float(p.mean() - t.mean()) * 100
        brier = float(((p - t) ** 2).mean())
        logger.info(f"   {name:6s} · pred={p.mean()*100:.1f}% real={t.mean()*100:.1f}% "
                    f"· gap={gaps[name]:+.1f} pp · Brier={brier:.3f}")
    return gaps


def save_models(models):
    for name, fname in [('1x2', 'model_1x2.pkl'), ('over15', 'model_over15.pkl'),
                        ('over25', 'model_over25.pkl'), ('over35', 'model_over35.pkl'),
                        ('btts', 'model_btts.pkl')]:
        joblib.dump(models[name], fname)
    logger.info("💾 Modelos guardados (model_*.pkl)")


def _lin_recalib(pairs):
    """Mínimos cuadrados p -> y sobre lista de (p, y). Devuelve (alpha, beta)."""
    n = len(pairs)
    if n < 10:
        return 0.0, 1.0
    mx = sum(p for p, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((p - mx) ** 2 for p, _ in pairs)
    if sxx <= 1e-9:
        return 0.0, 1.0
    beta = sum((p - mx) * (y - my) for p, y in pairs) / sxx
    return my - beta * mx, beta


def _apply_recalib(p, alpha, beta):
    return max(0.03, min(0.97, alpha + beta * p))


def validate_against_picks(models):
    """Compara ANTIGUO vs NUEVO sobre picks reales SIMULANDO PRODUCCIÓN:
    prob cruda + Capa B (recalibración lineal) ajustada FUERA DE MUESTRA
    (cross-fit 4 folds). El gap crudo en picks siempre es positivo por sesgo
    de selección; lo que decide es el gap post-recalibración y el Brier."""
    from stats_tracker import StatsTracker
    tr = StatsTracker()
    if not tr.enabled:
        logger.warning("Supabase no configurado: sin gate de picks")
        return None
    rows = []
    for p in tr.get_all_picks():
        if p.get('status') not in ('won', 'lost') or p.get('prob_ia') is None:
            continue
        f = p.get('features') or p.get('Features')
        if not f:
            continue
        try:
            X = pd.DataFrame([{c: float(f[c]) for c in FEATURE_COLS}])
        except Exception:
            continue
        merc = (p.get('mercado') or '').lower()
        pred = None
        if 'over 1.5' in merc:
            pred = models['over15'].predict_proba(X)[0][1]
        elif 'over 2.5' in merc:
            pred = models['over25'].predict_proba(X)[0][1]
        elif 'over 3.5' in merc:
            pred = models['over35'].predict_proba(X)[0][1]
        elif merc.startswith('btts'):
            pb = models['btts'].predict_proba(X)[0][1]
            pred = (1 - pb) if 'no' in merc else pb
        elif merc.startswith('1x2'):
            probs = models['1x2'].predict_proba(X)[0]
            part = p.get('partido') or ''
            home = part.split(' vs ')[0].lower() if ' vs ' in part else ''
            away = part.split(' vs ')[1].lower() if ' vs ' in part else ''
            if 'empate' in merc or 'draw' in merc:
                pred = probs[1]
            elif home and home in merc:
                pred = probs[0]
            elif away and away in merc:
                pred = probs[2]
        if pred is None:
            continue
        rows.append((float(p['prob_ia']), float(pred),
                     1.0 if p['status'] == 'won' else 0.0))
    n = len(rows)
    if n < 40:
        logger.warning(f"Picks con features insuficientes ({n}): sin gate de picks")
        return None

    old_raw = [r[0] for r in rows]
    new_raw = [r[1] for r in rows]
    ys = [r[2] for r in rows]
    g_old_raw = (sum(old_raw) / n - sum(ys) / n) * 100
    g_new_raw = (sum(new_raw) / n - sum(ys) / n) * 100

    # Cross-fit 4 folds: la Capa B de cada fold se ajusta SIN ese fold
    folds = 4
    old_recal = [0.0] * n
    new_recal = [0.0] * n
    for k in range(folds):
        tr_idx = [i for i in range(n) if i % folds != k]
        te_idx = [i for i in range(n) if i % folds == k]
        a_o, b_o = _lin_recalib([(old_raw[i], ys[i]) for i in tr_idx])
        a_n, b_n = _lin_recalib([(new_raw[i], ys[i]) for i in tr_idx])
        for i in te_idx:
            old_recal[i] = _apply_recalib(old_raw[i], a_o, b_o)
            new_recal[i] = _apply_recalib(new_raw[i], a_n, b_n)

    g_old = (sum(old_recal) / n - sum(ys) / n) * 100
    g_new = (sum(new_recal) / n - sum(ys) / n) * 100
    b_old = sum((old_recal[i] - ys[i]) ** 2 for i in range(n)) / n
    b_new = sum((new_recal[i] - ys[i]) ** 2 for i in range(n)) / n

    logger.info(f"🎯 VALIDACIÓN SOBRE {n} PICKS REALES (crudo → post-Capa B fuera de muestra)")
    logger.info(f"   ANTIGUO: crudo {g_old_raw:+.1f} pp → recalib {g_old:+.1f} pp · Brier {b_old:.3f}")
    logger.info(f"   NUEVO:   crudo {g_new_raw:+.1f} pp → recalib {g_new:+.1f} pp · Brier {b_new:.3f}")
    ok = abs(g_new) <= GATE_PP and b_new < b_old
    logger.info("   ✅ Gate de picks: OK" if ok else "   ❌ Gate de picks: FALLA")
    return ok


def write_deploy_marker():
    try:
        from stats_tracker import StatsTracker
        tr = StatsTracker()
        if not tr.enabled:
            return
        tr.client.table('meta').upsert(
            {'key': 'model_deployed_at',
             'value': datetime.now(timezone.utc).isoformat()},
            on_conflict='key').execute()
        logger.info("🏷️ Marcador model_deployed_at escrito en meta")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo escribir model_deployed_at: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--no-save', action='store_true')
    args = ap.parse_args()

    results = download_all()
    logger.info(f"⚽ {len(results)} partidos descargados")
    X, y, seasons = build_dataset(results)
    tr_mask = seasons != TEST_SEASON
    logger.info(f"🧮 Dataset: {len(X)} filas (train={int(tr_mask.sum())}, "
                f"test={int((~tr_mask).sum())})")

    models = train_all(X[tr_mask], y[tr_mask])
    gaps = evaluate(models, X[~tr_mask], y[~tr_mask])

    ok_eval = all(abs(g) <= GATE_PP for g in gaps.values())
    logger.info(f"🧪 Gate temporal: {'OK' if ok_eval else 'FALLA'} (todos |gap| ≤ {GATE_PP:.0f} pp)")

    ok_pick = validate_against_picks(models) if args.validate else None
    ok = ok_eval and (ok_pick if ok_pick is not None else True)

    if not args.no_save:
        save_models(models)

    if ok:
        with open('deploy_ok.txt', 'w') as f:
            f.write('ok')
        write_deploy_marker()
        logger.info("✅ VALIDACIÓN SUPERADA → el workflow commiteará los modelos")
    else:
        logger.info("❌ VALIDACIÓN NO SUPERADA → los modelos NO se commitearán")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
