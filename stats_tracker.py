"""
Módulo de persistencia y estadísticas para Value Bets.
Backend: Supabase (PostgreSQL gestionado, tier gratuito).
Lee SUPABASE_URL_FB / SUPABASE_KEY_FB (con fallback a SUPABASE_URL /
SUPABASE_KEY) para poder convivir con otras variables de otros proyectos.
Si no hay credenciales, queda desactivado sin romper la aplicación.
"""
import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict

logger = logging.getLogger(__name__)


class StatsTracker:
    def __init__(self, table_name: str = "picks"):
        self.table = table_name
        self.client = self._connect()

    def _connect(self):
        # Añadimos SUPABASE_ANON_KEY para que coincida con el nombre estándar de Supabase
        url = os.getenv("SUPABASE_URL_FB") or os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY_FB") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
        
        if not url or not key:
            logger.warning("⚠️ Credenciales Supabase no configuradas. Estadísticas desactivadas.")
            return None
       
        try:
            from supabase import create_client
            logger.info(f"✅ Conectado a Supabase (tabla: {self.table})")
            return create_client(url, key)
        except Exception as e:
            logger.error(f"❌ Error conectando a Supabase: {e}")
            return None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @staticmethod
    def _f(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    def _hash_pick(self, pick: Dict) -> str:
        key = f"{pick.get('Partido', '')}_{pick.get('Mercado', '')}_{pick.get('Hora', '')}"
        return hashlib.md5(key.encode()).hexdigest()

    # ==========================================
    # ESCRITURA
    # ==========================================
    def register_pick(self, pick: Dict) -> bool:
        if not self.client:
            return False
        try:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "liga": pick.get('Liga'),
                "partido": pick.get('Partido'),
                "hora": pick.get('Hora'),
                "mercado": pick.get('Mercado'),
                "cuota": self._f(pick.get('Cuota')),
                "prob_ia": self._f(pick.get('Prob. IA')),
                "prob_casa": self._f(pick.get('Prob. Casa')),
                "ev_percentage": self._f(pick.get('EV (%)')),
                "fuente": pick.get('Fuente'),
                "telegram_message_id": pick.get('Telegram Msg ID'),
                "features": pick.get('Features'),
                "raw_hash": self._hash_pick(pick),
            }
            self.client.table(self.table).upsert(
                payload, on_conflict="raw_hash", ignore_duplicates=True
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error registrando pick: {e}")
            return False

    def settle_pick(self, pick_id: int, status: str) -> bool:
        if not self.client:
            return False
        if status not in ['won', 'lost', 'void']:
            raise ValueError(f"Status inválido: {status}")
        try:
            self.client.table(self.table).update({
                "status": status,
                "settled_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", pick_id).execute()
            logger.info(f"Pick {pick_id} liquidado: {status}")
            return True
        except Exception as e:
            logger.error(f"Error liquidando pick: {e}")
            return False

    def clear_all(self) -> bool:
        """Borra todas las filas de la tabla (solo para tests)."""
        if not self.client:
            return False
        try:
            self.client.table(self.table).delete().neq("id", 0).execute()
            return True
        except Exception as e:
            logger.error(f"Error limpiando tabla: {e}")
            return False

    # ==========================================
    # LECTURA
    # ==========================================
    def hash_pick(self, pick):
        """Hash público de un pick (misma fórmula que el interno)."""
        return self._hash_pick(pick)

    def get_registered_hashes(self):
        """Set de raw_hash ya registrados (para no re-alertar en scans posteriores)."""
        if not self.client:
            return set()
        try:
            resp = self.client.table(self.table).select('raw_hash').execute()
            return {r['raw_hash'] for r in resp.data}
        except Exception as e:
            logger.error(f"Error leyendo hashes: {e}")
            return set()

    
    def get_all_picks(self) -> List[Dict]:
        if not self.client:
            return []
        try:
            resp = self.client.table(self.table).select("*").order("timestamp", desc=True).execute()
            return resp.data or []
        except Exception as e:
            logger.error(f"Error leyendo picks: {e}")
            return []

    # ==========================================
    # MÉTRICAS
    # ==========================================
    def calculate_stats(self) -> Dict:
        picks = self.get_all_picks()
        if not picks:
            return {'total': 0, 'settled': 0, 'pending': 0, 'message': 'Sin datos aún'}

        settled = [p for p in picks if p['status'] in ['won', 'lost']]

        if not settled:
            return {
                'total': len(picks), 'settled': 0, 'pending': len(picks),
                'wins': 0, 'losses': 0,
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
                brier_scores.append((p['prob_ia'] - resultado) ** 2)
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
            'calibration_gap': (avg_ev_declared - yield_pct) if avg_ev_declared is not None else None
        }

    def get_stats_by_market(self) -> Dict[str, Dict]:
        settled = [p for p in self.get_all_picks() if p['status'] in ['won', 'lost']]
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
                'Total': total, 'Aciertos': data['won'], 'Errores': data['lost'],
                'Hit %': round((data['won'] / total * 100), 1) if total > 0 else 0,
                'PnL': round(data['pnl'], 2),
                'Yield %': round((data['pnl'] / total * 100), 1) if total > 0 else 0,
                'Cuota media': round(avg_odd, 2) if avg_odd else 0
            }
        return result

    def get_stats_by_league(self) -> Dict[str, Dict]:
        settled = [p for p in self.get_all_picks() if p['status'] in ['won', 'lost']]
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
                'Total': total, 'Aciertos': data['won'], 'Errores': data['lost'],
                'Hit %': round((data['won'] / total * 100), 1) if total > 0 else 0,
                'PnL': round(data['pnl'], 2)
            }
        return result
