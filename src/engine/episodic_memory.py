"""
episodic_memory.py — Ultra-light vector memory with disk persistence.

Architecture
────────────
  EpisodicMemory    :  Dict-backed vector store, JSON-serialized to disk.
                       Uses the same hashing-trick as FeaturePipeline for
                       zero-dependency cosine similarity search.

Critical events (order imbalance > 3.0x OR price move > 1.5σ) are stored
as compressed records — vector + metadata + outcome label — and used by
BrainAgent.background_retrain() for asymmetric-loss training.
"""

import json
import logging
import math
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# Feature dimension must match quantum_brain.N_FEATURES
N_FEATURES = 50
MEMORY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "memory")


def _embed_snapshot(snapshot: dict) -> np.ndarray:
    """Lightweight hashing-trick embedder matching FeaturePipeline style.

    Produces a unit-normalized vector of dimension N_FEATURES from
    the snapshot's numeric and categorical fields, so cosine similarity
    against knowledge-block embeddings is meaningful.
    """
    NUMERIC_KEYS = [
        'price', 'change_pct', 'vwap', 'price_vwap_dist',
        'rsi', 'macd', 'macd_signal', 'macd_hist', 'bb_position', 'atr',
        'ema_20', 'ema_50',
        'delta', 'delta_accel', 'cvd', 'buy_volume', 'sell_volume',
        'volume', 'avg_volume', 'ba_ratio', 'imbalance', 'depth_imb_pct',
        'cumulative_delta',
        'kaufman_eff', 'spread_velocity', 'tick_speed', 'cancel_rate',
        'skewness', 'pinam',
        'rsi_5m', 'rsi_15m', 'confluence_score',
        'wall_bid_size', 'wall_ask_size', 'liq_zones',
        'directional_probability', 'confidence',
    ]
    CATEGORICAL_MAP = {
        'trend': {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'NEUTRAL': 0.0},
        'signal_text': {'LONG': 1.0, 'SHORT': -1.0, 'WAIT': 0.0},
        'market_bias': {'ALZA': 1.0, 'BAJA': -1.0, 'INCIERTO': 0.0},
    }

    vec = np.zeros(N_FEATURES, dtype=np.float32)
    for i, key in enumerate(NUMERIC_KEYS):
        val = snapshot.get(key, 0)
        if val is None:
            val = 0.0
        vec[i] = float(val)

    offset = len(NUMERIC_KEYS)
    for j, (key, mapping) in enumerate(CATEGORICAL_MAP.items()):
        raw = snapshot.get(key, '')
        vec[offset + j] = mapping.get(raw, 0.0)

    norm = np.linalg.norm(vec)
    if norm > 1e-8:
        vec /= norm
    return vec


def _critical_event(snapshot: dict) -> bool:
    """Return True if this snapshot represents a critical market event.

    Criteria (either):
      - Order imbalance > 3.0x
      - |price change| > 1.5 running σ of recent price changes
    """
    imb = snapshot.get('imbalance', 0)
    if abs(imb) > 3.0:
        return True
    change = snapshot.get('change_pct', 0)
    if abs(change) > 0.5:
        return True
    return False


class MemoryRecord:
    """A single compressed episodic memory record."""

    __slots__ = ('timestamp', 'price', 'dpoc', 'cum_vol', 'z_scores',
                 'label', 'vector', 'direction', 'confidence', 'delta',
                 'trap_status', 'bb_position', 'rsi', 'atr')

    def __init__(self, snapshot: dict, vector: np.ndarray,
                 label: str = 'PENDING'):
        self.timestamp: float = snapshot.get('_snapshot_time', time.time())
        self.price: float = float(snapshot.get('price', 0))
        self.dpoc: float = float(snapshot.get('dpoc', 0))
        self.cum_vol: float = float(snapshot.get('volume', 0))
        self.z_scores: List[float] = []
        self.label: str = label
        self.vector: np.ndarray = vector
        self.direction: str = snapshot.get('brain_direction',
                                           snapshot.get('signal_text', 'WAIT'))
        self.confidence: float = float(snapshot.get('brain_confidence_pct',
                                                     snapshot.get('confidence', 0)))
        self.delta: float = float(snapshot.get('delta', 0))
        self.trap_status: str = snapshot.get('trap_status', 'SIN TRAMPA')
        self.bb_position: float = float(snapshot.get('bb_position', 50))
        self.rsi: float = float(snapshot.get('rsi', 50))
        self.atr: float = float(snapshot.get('atr', 10))

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__
                if hasattr(self, s) and s != 'vector'}

    def to_compact(self) -> dict:
        d = self.to_dict()
        d['vec'] = self.vector.tolist()
        return d


class EpisodicMemory:
    """Ultra-light vector memory with json-lines disk persistence.

    Stores critical-event records indexed by a numpy vector for cosine
    similarity search.  Automatically persists to ``memory/`` on disk.

    Usage
    -----
        mem = EpisodicMemory()
        mem.store(snapshot)                 # if critical event
        nearest = mem.search(snapshot, k=3) # top-3 similar past events
    """

    def __init__(self, max_records: int = 5000, persist_path: str = None):
        self.max_records = max_records
        self._records: deque = deque(maxlen=max_records)
        self._vectors: List[np.ndarray] = []
        self._path = Path(persist_path or MEMORY_DIR)
        self._path.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── Public API ─────────────────────────────────────────────────────

    def store(self, snapshot: dict) -> Optional[MemoryRecord]:
        """Store snapshot as a memory record if it is a critical event.

        Returns the record, or None if no event triggered.
        """
        if not _critical_event(snapshot):
            return None
        vec = _embed_snapshot(snapshot)
        rec = MemoryRecord(snapshot, vec)
        self._records.append(rec)
        self._vectors.append(vec)
        self._trim()
        return rec

    def store_forced(self, snapshot: dict,
                     label: str = 'PENDING') -> MemoryRecord:
        """Unconditional store (for post-trade logging)."""
        vec = _embed_snapshot(snapshot)
        rec = MemoryRecord(snapshot, vec, label=label)
        self._records.append(rec)
        self._vectors.append(vec)
        self._trim()
        return rec

    def search(self, query_vec: np.ndarray, k: int = 3,
               min_sim: float = 0.15) -> List[dict]:
        """Cosine-similarity search for top-k similar past records.

        Parameters
        ----------
        query_vec : np.ndarray (N_FEATURES,)
            Query vector (e.g. from _embed_snapshot).
        k : int
            Number of results (max).
        min_sim : float
            Minimum cosine similarity threshold.

        Returns list of dicts with keys: record (MemoryRecord), similarity.
        """
        if not self._vectors:
            return []
        stack = np.stack(self._vectors, axis=0)
        dots = stack @ query_vec
        norms = np.linalg.norm(stack, axis=1) * np.linalg.norm(query_vec)
        sims = np.divide(dots, norms, out=np.zeros_like(dots),
                         where=norms > 1e-8)

        top_idx = np.argsort(sims)[::-1][:k]
        results = []
        for idx in top_idx:
            if idx >= len(self._records):
                continue
            sim = float(sims[idx])
            if sim < min_sim:
                continue
            results.append({
                'record': self._records[idx],
                'similarity': round(sim, 4),
            })
        return results

    def search_from_snapshot(self, snapshot: dict, k: int = 3,
                             min_sim: float = 0.15) -> List[dict]:
        """Convenience: embed snapshot then search."""
        vec = _embed_snapshot(snapshot)
        return self.search(vec, k=k, min_sim=min_sim)

    def label_last(self, label: str):
        """Label the most recent record (e.g. 'SUCCESS' / 'FAILED')."""
        if self._records:
            self._records[-1].label = label

    def label_by_timestamp(self, ts: float, label: str):
        """Label a record by its timestamp."""
        for rec in self._records:
            if abs(rec.timestamp - ts) < 1.0:
                rec.label = label
                return

    def get_failed(self) -> List[MemoryRecord]:
        """Return all records labelled FAILED."""
        return [r for r in self._records if r.label == 'FAILED']

    def get_success(self) -> List[MemoryRecord]:
        """Return all records labelled SUCCESS."""
        return [r for r in self._records if r.label == 'SUCCESS']

    def get_training_batch(self, max_samples: int = None) -> List[MemoryRecord]:
        """Return labelled records for training (FAILED weighted 3x)."""
        failed = self.get_failed()
        success = self.get_success()
        if max_samples:
            failed = failed[:max_samples // 4]
            success = success[:max_samples - len(failed)]
        return failed * 3 + success  # FAILED repeated 3x for weighting

    def stats(self) -> dict:
        return {
            'total_records': len(self._records),
            'failed': len(self.get_failed()),
            'success': len(self.get_success()),
            'pending': sum(1 for r in self._records if r.label == 'PENDING'),
        }

    def persist(self):
        """Write all records to disk as JSON lines."""
        path = self._path / "episodes.jsonl"
        with open(path, 'w') as f:
            for rec in self._records:
                f.write(json.dumps(rec.to_compact()) + '\n')
        log.info("[EpisodicMemory] Persisted %d records to %s",
                 len(self._records), path)

    # ── Private ────────────────────────────────────────────────────────

    def _trim(self):
        if len(self._vectors) > self.max_records:
            self._vectors = self._vectors[-self.max_records:]

    def _load(self):
        path = self._path / "episodes.jsonl"
        if not path.exists():
            return
        try:
            with open(path) as f:
                for line in f:
                    data = json.loads(line)
                    rec = MemoryRecord.__new__(MemoryRecord)
                    for k, v in data.items():
                        if k == 'vec':
                            rec.vector = np.array(v, dtype=np.float32)
                        else:
                            setattr(rec, k, v)
                    self._records.append(rec)
                    self._vectors.append(rec.vector)
            log.info("[EpisodicMemory] Loaded %d records from %s",
                     len(self._records), path)
        except Exception as e:
            log.warning("[EpisodicMemory] Load error: %s", e)
