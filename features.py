#!/usr/bin/env python3
"""
Construcción de features para el modelo XGBoost de value bets.
"""
import os
import pandas as pd
import numpy as np

TEAM_DB = "data/teams.csv"


def normalize_team_name(name):
    """Normaliza nombres de equipos para matching."""
    if not name:
        return ""
    return name.lower().strip()


def get_team_strength(teams_df, team_name):
    """Devuelve la fuerza del equipo (0-1) basado en datos históricos."""
    norm_name = normalize_team_name(team_name)
    
    # Buscar en la base de datos
    for _, row in teams_df.iterrows():
        if normalize_team_name(row.get('team', '')) == norm_name:
            return float(row.get('strength', 0.5))
    
    # Default si no se encuentra
    return 0.5


def build_features_for_match(teams_df, home_team, away_team, odds, mercado, point=None):
    """
    Construye el vector de features para un partido específico.
    
    Args:
        teams_df: DataFrame con datos de equipos
        home_team: Nombre del equipo local
        away_team: Nombre del equipo visitante
        odds: Cuota decimal
        mercado: Nombre del mercado (ej: "1X2 - Home Team")
        point: Valor de línea (ej: 2.5 para Over 2.5)
    
    Returns:
        DataFrame con una fila de features, o None si no se puede construir
    """
    try:
        # Probabilidad implícita de la cuota
        implied_prob = 1 / odds if odds > 0 else 0
        
        # Fuerza de los equipos
        strength_home = get_team_strength(teams_df, home_team)
        strength_away = get_team_strength(teams_df, away_team)
        
        # ¿Es el equipo local el favorito?
        is_home = 1 if home_team in mercado else 0
        
        # Tipo de mercado (categórico codificado)
        market_lower = mercado.lower()
        if '1x2' in market_lower:
            market_type = 0
        elif 'over' in market_lower or 'under' in market_lower:
            market_type = 1
        elif 'btts' in market_lower or 'both teams' in market_lower:
            market_type = 2
        elif 'double chance' in market_lower:
            market_type = 3
        else:
            market_type = 4
        
        # Valor de línea (para mercados de goles)
        point_value = float(point) if point is not None else 0.0
        
        # Construir DataFrame
        features = pd.DataFrame([{
            "implied_prob": implied_prob,
            "team_strength_home": strength_home,
            "team_strength_away": strength_away,
            "is_home": is_home,
            "market_type": market_type,
            "point_value": point_value,
        }])
        
        return features
        
    except Exception as e:
        print(f"Error construyendo features: {e}")
        return None
