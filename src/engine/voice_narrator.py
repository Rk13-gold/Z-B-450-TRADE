"""
voice_narrator.py — Generación de voz para "El Trader" usando Edge TTS.

Arquitectura
────────────
  VoiceNarrator recibe un NarrationScript del ScriptGenerator y usa
  Edge TTS (Microsoft) para generar audio profesional en español.

  Edge TTS es una biblioteca que utiliza el servicio de texto a voz
  de Microsoft Edge, proporcionando voces neuronales de alta calidad.

  Voz usada: es-MX-DaliaNeural (español mexicano, voz femenina profesional)

Flujo
─────
  1. ScriptGenerator → NarrationScript (con optimized_tts)
  2. VoiceNarrator.generate_audio(script) → .mp3 file
  3. Audio guardado en: audio/{YYYY}/{MM}/{DD}/{EVENT_ID}.mp3
  4. Reproducción en vivo opcional en el dashboard
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.engine.script_generator import NarrationScript

log = logging.getLogger(__name__)

# ── Configuración de voz ────────────────────────────────────────────
VOICE = "es-MX-DaliaNeural"
RATE = "+0%"      # velocidad normal
VOLUME = "+0%"    # volumen normal
PITCH = "+0Hz"    # tono normal

AUDIO_BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "audio")


class VoiceNarrator:
    """Genera y reproduce audio narrativo usando Edge TTS.

    Uso
    ---
        narrator = VoiceNarrator()
        path = await narrator.generate_audio(script)
        await narrator.play_live(path)
    """

    def __init__(self, db_path: str = None):
        self._audio_dir = AUDIO_BASE_DIR
        os.makedirs(self._audio_dir, exist_ok=True)
        self._db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "bb450_trades.db"
        )
        self._init_db()
        self._last_audio_path: Optional[str] = None
        self._tts_engine = None

    def _init_db(self):
        """Crea la tabla de audio si no existe."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audio_narrations (
                    id TEXT PRIMARY KEY,
                    event_type TEXT,
                    timestamp REAL,
                    price REAL,
                    title TEXT,
                    audio_path TEXT,
                    duration_seconds INTEGER,
                    created_at TEXT,
                    played INTEGER DEFAULT 0
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[VoiceNarrator] DB init error: {e}")

    def _get_output_path(self, event_id: str) -> str:
        """Genera la ruta: audio/{YYYY}/{MM}/{DD}/{EVENT_ID}.mp3"""
        now = datetime.now(timezone.utc)
        date_path = now.strftime("%Y/%m/%d")
        full_dir = os.path.join(self._audio_dir, date_path)
        os.makedirs(full_dir, exist_ok=True)
        return os.path.join(full_dir, f"{event_id}.mp3")

    async def generate_audio(self, script: NarrationScript) -> Optional[str]:
        """Genera el archivo de audio usando Edge TTS.

        Parameters
        ----------
        script : NarrationScript
            El guión generado, debe contener optimized_tts.

        Returns
        -------
        str | None
            Ruta absoluta al archivo .mp3 generado, o None si falla.
        """
        text = script.optimized_tts or script.raw_text
        if not text:
            log.warning("[VoiceNarrator] No text to synthesize")
            return None

        output_path = self._get_output_path(script.event_id)

        try:
            import edge_tts

            communicate = edge_tts.Communicate(
                text,
                voice=VOICE,
                rate=RATE,
                volume=VOLUME,
                pitch=PITCH,
            )

            await communicate.save(output_path)

            if not os.path.exists(output_path):
                log.error(f"[VoiceNarrator] File not created: {output_path}")
                return None

            file_size = os.path.getsize(output_path)
            if file_size < 100:
                log.warning(f"[VoiceNarrator] Audio file too small ({file_size} bytes): {output_path}")
                os.remove(output_path)
                return None

            log.info(f"[VoiceNarrator] Audio generated: {output_path} ({file_size/1024:.1f} KB)")

            script.audio_path = output_path
            self._last_audio_path = output_path
            self._save_to_db(script, output_path)

            return output_path

        except ImportError:
            log.error("[VoiceNarrator] edge_tts not installed. Run: pip install edge-tts")
            return None
        except Exception as e:
            log.error(f"[VoiceNarrator] TTS error: {e}")
            return None

    def _save_to_db(self, script: NarrationScript, audio_path: str):
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            conn.execute(
                """INSERT OR REPLACE INTO audio_narrations
                   (id, event_type, timestamp, price, title, audio_path, duration_seconds, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    script.event_id,
                    script.event_type,
                    script.timestamp,
                    script.price,
                    script.title,
                    audio_path,
                    script.duration_seconds,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[VoiceNarrator] DB save error: {e}")

    async def play_live(self, audio_path: str) -> bool:
        """Reproduce el audio en vivo usando el reproductor del sistema.

        Parameters
        ----------
        audio_path : str
            Ruta al archivo .mp3 a reproducir.

        Returns
        -------
        bool
            True si se pudo reproducir.
        """
        if not os.path.exists(audio_path):
            log.warning(f"[VoiceNarrator] Audio file not found: {audio_path}")
            return False

        try:
            import subprocess
            import sys

            if sys.platform == "darwin":
                proc = await asyncio.create_subprocess_exec(
                    "afplay", audio_path,
                    stdout=asyncio.DEVNULL, stderr=asyncio.DEVNULL,
                )
                await proc.wait()
            elif sys.platform == "linux":
                proc = await asyncio.create_subprocess_exec(
                    "paplay", audio_path,
                    stdout=asyncio.DEVNULL, stderr=asyncio.DEVNULL,
                )
                await proc.wait()
            elif sys.platform == "win32":
                import winsound
                winsound.PlaySound(audio_path, winsound.SND_FILENAME)

            log.info(f"[VoiceNarrator] Audio played: {audio_path}")
            return True

        except Exception as e:
            log.error(f"[VoiceNarrator] Playback error: {e}")
            return False

    async def narrate(self, script: NarrationScript) -> bool:
        """Proceso completo: generar audio + reproducir en vivo.

        Parameters
        ----------
        script : NarrationScript
            Guión a narrar.

        Returns
        -------
        bool
            True si se generó y reprodujo correctamente.
        """
        audio_path = await self.generate_audio(script)
        if not audio_path:
            return False

        played = await self.play_live(audio_path)
        return played

    def get_last_audio(self) -> Optional[str]:
        return self._last_audio_path

    def get_audio_history(self, limit: int = 10) -> list[dict]:
        """Obtiene el historial de audios generados.

        Returns
        -------
        list[dict]
            Lista de registros de audio con metadatos.
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audio_narrations ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning(f"[VoiceNarrator] History error: {e}")
            return []

    def get_stats(self) -> dict:
        """Estadísticas de audio generado."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=3)
            total = conn.execute("SELECT COUNT(*) FROM audio_narrations").fetchone()[0]
            total_size = conn.execute(
                "SELECT COUNT(*) FROM audio_narrations WHERE played = 1"
            ).fetchone()[0]
            conn.close()
            return {"total_generated": total, "total_played": total_size}
        except Exception:
            return {"total_generated": 0, "total_played": 0}


voice_narrator = VoiceNarrator()
