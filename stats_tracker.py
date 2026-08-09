"""
Módulo de persistencia y estadísticas para Value Bets.
Guarda cada pick en SQLite y calcula métricas históricas.
"""
import sqlite3
import hashlib
from datetime import datetime, timezone
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

class StatsTracker:
    def __init__(self, db_path="picks_history.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Inicializa la base de datos si no existe."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS picks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    liga TEXT,
                    partido TEXT,
                    hora TEXT,
                    mercado TEXT,
                    cuota REAL,
                    prob_ia REAL,
                    prob_casa REAL,
                    ev_percentage REAL,
                    fuente TEXT,
                    status TEXT DEFAULT 'pending',
                    settled_at TEXT,
                    raw_hash TEXT UNIQUE
                )
            """)
            conn.commit()
        logger.info(f"✅ Base de datos inicializada: {self.db_path}")
    
    def _hash_pick(self, pick: Dict) -> str:
        """Genera hash único para evitar duplicados."""
        key = f"{pick.get('Partido', '')}_{pick.get('Mercado', '')}_{pick.get('Hora', '')}"
        return hashlib.md5(key.encode()).hexdigest()
    
    def register_pick(self, pick: Dict) -> bool:
        """Registra un pick en la base de datos."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                raw_hash = self._hash_pick(pick)
                conn.execute("""
                    INSERT OR IGNORE INTO picks 
                    (timestamp, liga, partido, hora, mercado, cuota, 
                     prob_ia, prob_casa, ev_percentage, fuente, raw_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    pick.get('Liga'),
                    pick.get('Partido'),
                    pick.get('Hora'),
                    pick.get('Mercado'),
                    pick.get('Cuota'),
                    pick.get('Prob. IA'),
                    pick.get('Prob. Casa'),
                    pick.get('EV (%)'),
                    pick.get('Fuente'),
                    raw_hash
                ))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error registrando pick: {e}")
            return False
    
    def get_all_picks(self) -> List[Dict]:
        """Recupera todos los picks."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM picks ORDER BY timestamp DESC")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error leyendo picks: {e}")
            return []
    
    def settle_pick(self, pick_id: int, status: str) -> bool:
        """Liquida un pick: won, lost, void."""
        if status not in ['won', 'lost', 'void']:
            raise ValueError(f"Status inválido: {status}")
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE picks 
                    SET status = ?, settled_at = ?
                    WHERE id = ?
                """, (status, datetime.now(timezone.utc).isoformat(), pick_id))
                conn.commit()
                logger.info(f"Pick {pick_id} liquidado: {status}")
                return True
        except Exception as e:
            logger.error(f"Error liquidando pick: {e}")
            return False
    
    def calculate_stats(self) -> Dict:
        """Calcula estadísticas históricas."""
        picks = self.get_all_picks()
        if not picks:
            return {'total': 0, 'settled': 0, 'pending': 0, 'message': 'Sin datos aún'}
        
        settled = [p for p in picks if p['status'] in ['won', 'lost']]
        
        if not settled:
            return {
                'total': len(picks),
                'settled': 0,
                'pending': len(picks),
                'wins': 0,
                'losses': 0,
                'message': 'Picks registrados pero no liquidados aún'
            }
        
        wins = sum(1 for p in settled if p['status'] == 'won')
        losses = sum(1 for p in settled if p['status'] == 'lost')
        
        pnl = sum(
            (p['cuota'] - 1) if p['status'] == 'won' else -1
            for p in settled if p['cuota']
        )
        
        yield_pct = (pnl / len(settled)) * 100 if settled else 0
        hit_rate = (wins / len(settled)) * 100 if settled else 0
        
        brier_scores = []
        for p in settled:
            if p['prob_ia'] is not None:
                resultado = 1 if p['status'] == 'won' else 0
                brier = (p['prob_ia'] - resultado) ** 2
                brier_scores.append(brier)
        
        avg_brier = sum(brier_scores) / len(brier_scores) if brier_scores else None
        
        ev_values = [p['ev_percentage'] for p in settled if p['ev_percentage'] is not None]
        avg_ev_declared = sum(ev_values) / len(ev_values) if ev_values else None
        
        return {
            'total': len(picks),
            'settled': len(settled),
            'pending': len([p for p in picks if p['status'] == 'pending']),
            'wins': wins,
            'losses': losses,
            'hit_rate': hit_rate,
            'pnl': pnl,
            'yield': yield_pct,
            'avg_ev_declared': avg_ev_declared,
            'brier_score': avg_brier,
            'calibration_gap': (avg_ev_declared - yield_pct) if (avg_ev_declared is not None) else None
        }
    
    def get_stats_by_market(self) -> Dict[str, Dict]:
        """Estadísticas agrupadas por mercado."""
        picks = self.get_all_picks()
        settled = [p for p in picks if p['status'] in ['won', 'lost']]
        
        markets = {}
        for p in settled:
            mercado = p['mercado'] or 'Desconocido'
            if mercado not in markets:
                markets[mercado] = {'won': 0, 'lost': 0, 'pnl': 0, 'cuotas': []}
            
            if p['status'] == 'won':
                markets[mercado]['won'] += 1
                if p['cuota']:
                    markets[mercado]['pnl'] += p['cuota'] - 1
                    markets[mercado]['cuotas'].append(p['cuota'])
            else:
                markets[mercado]['lost'] += 1
                markets[mercado]['pnl'] -= 1
        
        result = {}
        for mercado, data in markets.items():
            total = data['won'] + data['lost']
            avg_odd = sum(data['cuotas']) / len(data['cuotas']) if data['cuotas'] else 0
            result[mercado] = {
                'Total': total,
                'Aciertos': data['won'],
                'Errores': data['lost'],
                'Hit %': round((data['won'] / total * 100), 1) if total > 0 else 0,
                'PnL': round(data['pnl'], 2),
                'Yield %': round((data['pnl'] / total * 100), 1) if total > 0 else 0,
                'Cuota media': round(avg_odd, 2) if avg_odd else 0
            }
        return result
    
    def get_stats_by_league(self) -> Dict[str, Dict]:
        """Estadísticas agrupadas por liga."""
        picks = self.get_all_picks()
        settled = [p for p in picks if p['status'] in ['won', 'lost']]
        
        leagues = {}
        for p in settled:
            liga = p['liga'] or 'Desconocida'
            if liga not in leagues:
                leagues[liga] = {'won': 0, 'lost': 0, 'pnl': 0}
            
            if p['status'] == 'won':
                leagues[liga]['won'] += 1
                if p['cuota']:
                    leagues[liga]['pnl'] += p['cuota'] - 1
            else:
                leagues[liga]['lost'] += 1
                leagues[liga]['pnl'] -= 1
        
        result = {}
        for liga, data in leagues.items():
            total = data['won'] + data['lost']
            result[liga] = {
                'Total': total,
                'Aciertos': data['won'],
                'Errores': data['lost'],
                'Hit %': round((data['won'] / total * 100), 1) if total > 0 else 0,
                'PnL': round(data['pnl'], 2)
            }
        return result
