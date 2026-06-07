import sqlite3
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from config.settings import settings


class SupabaseManager:
    def __init__(self):
        self.db_path = "bb450_trades.db"
        self.conn = None
        self._init_db()

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                quantity REAL,
                pnl REAL,
                entry_time TEXT,
                exit_time TEXT,
                duration_seconds INTEGER,
                status TEXT,
                outcome TEXT,
                closed_at TEXT,
                leverage_used INTEGER,
                created_at TEXT NOT NULL
            )
        ''')

        # ── Migración de columnas para DBs existentes ─────────────────────
        _new_cols = {
            "outcome":      "TEXT",
            "closed_at":    "TEXT",
            "leverage_used": "INTEGER",
        }
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(trades)")}
        for col, col_type in _new_cols.items():
            if col not in existing:
                cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                print(f"[DB] Columna '{col}' agregada a la tabla trades")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT,
                price REAL,
                reason TEXT,
                indicators TEXT,
                delta REAL,
                rsi REAL,
                created_at TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE,
                total_trades INTEGER,
                total_pnl REAL,
                win_rate REAL
            )
        ''')

        self.conn.commit()
        print(f"✅ Base de datos local inicializada: {self.db_path}")

    def connect(self):
        if not self.conn:
            self._init_db()
        print("✅ Conectado a SQLite local")
        return True

    async def save_trade(self, trade: Dict) -> bool:
        """Guarda un trade en la DB local con soporte completo de outcome/leverage."""
        try:
            cursor = self.conn.cursor()
            # Derivar outcome si no viene explícito
            outcome = trade.get('outcome')  # 'TP', 'SL', o None
            if outcome is None:
                pnl = trade.get('pnl', 0)
                if pnl is not None:
                    outcome = 'TP' if pnl > 0 else ('SL' if pnl < 0 else None)

            closed_at = (
                trade.get('closed_at')
                or trade.get('exit_time')
                or datetime.now().isoformat()
            )

            cursor.execute('''
                INSERT INTO trades (
                    symbol, side, entry_price, exit_price, quantity, pnl,
                    entry_time, exit_time, duration_seconds, status,
                    outcome, closed_at, leverage_used, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade.get('symbol', settings.SYMBOL),
                trade.get('side'),
                trade.get('entry_price'),
                trade.get('exit_price'),
                trade.get('quantity'),
                trade.get('pnl', 0),
                trade.get('entry_time'),
                trade.get('exit_time', datetime.now().isoformat()),
                trade.get('duration', 0),
                trade.get('status', 'closed'),
                outcome,
                closed_at,
                trade.get('leverage_used'),
                datetime.now().isoformat(),
            ))
            self.conn.commit()
            print(f"💾 Trade guardado: {trade.get('side')} | PnL: ${trade.get('pnl', 0):.2f} | outcome={outcome}")
            return True
        except Exception as e:
            print(f"❌ Error guardando trade: {e}")
            return False

    def close_trade(self, trade_id: int, outcome: str, exit_price: float,
                    pnl: float, leverage_used: int = 0) -> bool:
        """Actualiza un trade abierto con su resultado final (TP/SL).

        Llama a este método cuando el sistema detecta que un trade cerró,
        ya sea por TP, SL o cierre manual, para que el historial de
        apalancamiento reactivo quede correcto.

        Parameters
        ----------
        trade_id    : ID del trade (columna id de la tabla trades)
        outcome     : 'TP' o 'SL'
        exit_price  : precio de cierre
        pnl         : PnL realizado en USDT
        leverage_used: apalancamiento con el que se abrió
        """
        try:
            cursor = self.conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                """
                UPDATE trades
                SET outcome=?, closed_at=?, exit_price=?, pnl=?,
                    leverage_used=?, status='closed'
                WHERE id=?
                """,
                (outcome, now, exit_price, pnl, leverage_used or None, trade_id),
            )
            self.conn.commit()
            print(f"🔄 Trade #{trade_id} cerrado: {outcome} | PnL=${pnl:+.2f} | lev={leverage_used}x")
            return cursor.rowcount > 0
        except Exception as e:
            print(f"❌ Error cerrando trade #{trade_id}: {e}")
            return False

    async def save_signal(self, signal: Dict) -> bool:
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO signals (
                    symbol, signal_type, price, reason, indicators, delta, rsi, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                settings.SYMBOL,
                signal.get('signal'),
                signal.get('price'),
                signal.get('reason', ''),
                str(signal.get('indicators', {})),
                signal.get('delta', 0),
                signal.get('rsi', 0),
                datetime.now().isoformat()
            ))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"❌ Error guardando señal: {e}")
            return False

    async def get_trades(self, limit: int = 100) -> List[Dict]:
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT * FROM trades
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            print(f"❌ Error obteniendo trades: {e}")
            return []

    async def get_daily_stats(self) -> Dict:
        try:
            cursor = self.conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')

            cursor.execute('SELECT * FROM trades')
            trades = cursor.fetchall()

            if not trades:
                return {'total_trades': 0, 'total_pnl': 0, 'win_rate': 0}

            pnl_sum = sum(t['pnl'] for t in trades if t['pnl'])
            wins = sum(1 for t in trades if t['pnl'] and t['pnl'] > 0)
            win_rate = wins / len(trades) if trades else 0

            return {
                'total_trades': len(trades),
                'total_pnl': pnl_sum,
                'win_rate': win_rate
            }
        except Exception as e:
            print(f"❌ Error obteniendo estadísticas: {e}")
            return {'total_trades': 0, 'total_pnl': 0, 'win_rate': 0}

    def get_recent_trades(self, count: int = 10) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM trades
            ORDER BY created_at DESC
            LIMIT ?
        ''', (count,))
        return [dict(row) for row in cursor.fetchall()]

    def get_pnl_summary(self) -> Dict:
        cursor = self.conn.cursor()

        cursor.execute('SELECT SUM(pnl) as total FROM trades')
        total_pnl = cursor.fetchone()['total'] or 0

        cursor.execute('SELECT COUNT(*) as count FROM trades')
        total_trades = cursor.fetchone()['count']

        cursor.execute('SELECT COUNT(*) as wins FROM trades WHERE pnl > 0')
        wins = cursor.fetchone()['wins']

        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        return {
            'total_pnl': total_pnl,
            'total_trades': total_trades,
            'wins': wins,
            'losses': total_trades - wins,
            'win_rate': win_rate
        }


supabase_manager = SupabaseManager()