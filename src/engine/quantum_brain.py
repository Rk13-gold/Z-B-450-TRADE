#!/usr/bin/env python3
"""
quantum_brain.py — Central AI Engine for BB-450 Trading Platform.

Architecture
────────────
  QuantumBrainNetwork  :  PyTorch nn.Module (Dense + LSTM → Softmax)
  FeaturePipeline      :  Snapshot dict → normalized torch.Tensor
  BrainAgent           :  Async inference manager + Telegram alert dispatch

Integrates with dashboard_gui.py via:
  brain_agent = BrainAgent(telegram_queue=bot._queue)
  decision = await brain_agent.evaluate_market_state(snapshot)
"""

import asyncio
import logging
import math
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from config.settings import settings
from src.engine.knowledge_index import KnowledgeIndex

# Episodic memory (lazy-imported to avoid circular deps at module level)
_episodic_memory: Optional['EpisodicMemory'] = None

def get_episodic_memory() -> 'EpisodicMemory':
    global _episodic_memory
    if _episodic_memory is None:
        from src.engine.episodic_memory import EpisodicMemory
        _episodic_memory = EpisodicMemory()
    return _episodic_memory

def persist_episodic_memory():
    mem = get_episodic_memory()
    try:
        mem.persist()
    except Exception:
        pass

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

ALERT_THRESHOLD = 0.85        # 85 % confidence → high-fidelity alert
COOLDOWN_SECONDS = 30         # min seconds between brain alerts
SMOOTHING_WINDOW = 5          # running average over last N predictions
HIDDEN_SIZE = 256
NUM_LAYERS = 2
DROPOUT = 0.35

# Directional hysteresis — only flip on conviction
HYSTERESIS_DELTA = 0.08       # 8 % absolute margin required to switch direction
LOW_CONF_COOLDOWN = 180       # seconds — block opposite signal when 50–60 % conf
EXHAUSTION_KEFF_THRESHOLD = 0.15  # max Kaufman Efficiency for exhaustion regime

# Training constants
LEARNING_RATE = 1e-4
WEIGHT_DECAY = 1e-5
RETRAIN_INTERVAL = 14400      # 4 hours in seconds
TRAUMA_LOSS_MULTIPLIER = 3.0  # asymmetric penalty for FAILED patterns
MIN_TRAIN_SAMPLES = 20        # minimum records before first retrain
MAX_TRAIN_SAMPLES = 500       # cap per retrain session
TRAIN_EPOCHS = 5              # epochs per retrain session

# ══════════════════════════════════════════════════════════════════════════════
# Feature specification — maps snapshot fields → tensor positions
# ══════════════════════════════════════════════════════════════════════════════

NUMERIC_FEATURES = [
    # Price & structure
    'price', 'change_pct', 'vwap', 'price_vwap_dist',
    'day_high', 'day_low',
    # Technical indicators
    'rsi', 'macd', 'macd_signal', 'macd_hist', 'bb_position', 'atr',
    'ema_20', 'ema_50',
    # Order flow (pure microstructural — no infra / app state)
    'delta', 'delta_accel', 'cvd', 'buy_volume', 'sell_volume',
    'volume', 'avg_volume', 'ba_ratio', 'imbalance', 'depth_imb_pct',
    'cumulative_delta',
    # Microstructure
    'kaufman_eff', 'spread_velocity', 'tick_speed', 'cancel_rate',
    'skewness', 'pinam',
    # Multi-timeframe
    'rsi_5m', 'rsi_15m', 'confluence_score',
    # Walls & liquidity
    'wall_bid_size', 'wall_ask_size', 'liq_zones',
    # Trap & confidence
    'directional_probability', 'confidence',
]

CATEGORICAL_MAP: Dict[str, Dict[str, float]] = {
    'trend':         {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'NEUTRAL': 0.0},
    'signal_text':   {'LONG': 1.0, 'SHORT': -1.0, 'WAIT': 0.0},
    'bb_squeeze':    {'SQUEEZE': 1.0, 'NORMAL': 0.0},
    'force':         {'BUY': 1.0, 'SELL': -1.0, 'NONE': 0.0},
    'trend_5m':      {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'WAIT': 0.0, 'NEUTRAL': 0.0},
    'trend_15m':     {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'WAIT': 0.0, 'NEUTRAL': 0.0},
    'trend_1h':      {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'WAIT': 0.0, 'NEUTRAL': 0.0},
    'trend_4h':      {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'WAIT': 0.0, 'NEUTRAL': 0.0},
    'market_bias':   {'ALZA': 1.0, 'BAJA': -1.0, 'INCIERTO': 0.0},
    'ema_cross_5m':  {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'NEUTRAL': 0.0},
    'ema_cross_15m': {'ALCISTA': 1.0, 'BAJISTA': -1.0, 'NEUTRAL': 0.0},
}

CATEGORICAL_FEATURES = list(CATEGORICAL_MAP.keys())
N_FEATURES = len(NUMERIC_FEATURES) + len(CATEGORICAL_FEATURES)  # 39 + 11 = 50


# ══════════════════════════════════════════════════════════════════════════════
# 1. NEURAL NETWORK
# ══════════════════════════════════════════════════════════════════════════════

class ResidualBlock(nn.Module):
    """Pre-activation residual block with LayerNorm."""

    def __init__(self, dim: int, dropout: float = DROPOUT):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.linear = nn.Linear(dim, dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.norm(x)
        out = F.relu(out)
        out = self.linear(out)
        out = self.dropout(out)
        return x + out


class QuantumBrainNetwork(nn.Module):
    """Hybrid Dense + LSTM network for market state classification.

    Architecture
    ────────────
      Input (N_FEATURES)
        → Linear(512) + LayerNorm + ReLU + Dropout
        → 2× ResidualBlock(512)
        → LSTM(512 → 256, 2 layers)   ← temporal processing
        → Linear(256 → 128) + LayerNorm + ReLU + Dropout
        → Linear(128 → 3) + LogSoftmax

    Output
    ──────
      log_probs ∈ ℝ³ : [ln P(ALZA), ln P(BAJA), ln P(INCIERTO)]
      hidden_state   : (hn, cn) for next call
    """

    def __init__(self, n_features: int = N_FEATURES,
                 hidden: int = HIDDEN_SIZE,
                 num_layers: int = NUM_LAYERS,
                 dropout: float = DROPOUT):
        super().__init__()
        self.n_features = n_features
        self.hidden_size = hidden
        self.num_layers = num_layers

        # Input projection
        self.input_proj = nn.Sequential(
            nn.Linear(n_features, hidden * 2),
            nn.LayerNorm(hidden * 2),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
        )

        # Residual blocks
        self.res_blocks = nn.ModuleList([
            ResidualBlock(hidden * 2, dropout) for _ in range(2)
        ])

        # LSTM for temporal processing
        self.lstm = nn.LSTM(
            input_size=hidden * 2,
            hidden_size=hidden,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
        )

        # Output head
        self.output_head = nn.Sequential(
            nn.Linear(hidden, hidden // 2),
            nn.LayerNorm(hidden // 2),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout * 0.5),
            nn.Linear(hidden // 2, 3),
        )

        self._init_weights()

    # ── Initialization ─────────────────────────────────────────────────

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight, mode='fan_in',
                                        nonlinearity='relu')
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.LayerNorm):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)

    # ── Forward ────────────────────────────────────────────────────────

    def forward(self, x: torch.Tensor,
                hidden_state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None
                ) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        Parameters
        ----------
        x : (B, N_FEATURES) or (N_FEATURES,)
        hidden_state : optional (h0, c0) for LSTM

        Returns
        -------
        log_probs     : (B, 3) or (3,) — log-softmax
        hidden_state  : (hn, cn) — for next call
        """
        squeeze_batch = x.dim() == 1
        if squeeze_batch:
            x = x.unsqueeze(0)

        x = self.input_proj(x)                          # (B, H*2)

        for block in self.res_blocks:
            x = block(x)                                 # (B, H*2)

        x = x.unsqueeze(1)                               # (B, 1, H*2)

        if hidden_state is not None:
            h, c = hidden_state
            h = h.detach()
            c = c.detach()
            lstm_state = (h, c)
        else:
            lstm_state = None

        lstm_out, (hn, cn) = self.lstm(x, lstm_state)
        lstm_out = lstm_out.squeeze(1)                   # (B, H)

        logits = self.output_head(lstm_out)               # (B, 3)
        log_probs = F.log_softmax(logits, dim=-1)

        if squeeze_batch:
            log_probs = log_probs.squeeze(0)

        return log_probs, (hn, cn)


# ══════════════════════════════════════════════════════════════════════════════
# 2. FEATURE PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class FeaturePipeline:
    """Snapshot dict → normalized torch.Tensor.

    Maintains running mean / std for online normalization and handles
    missing values, outlier clipping, and string → numeric encoding.
    """

    def __init__(self):
        self.n_features = N_FEATURES
        self.running_mean = torch.zeros(N_FEATURES)
        self.running_std = torch.ones(N_FEATURES)
        self.running_count = torch.zeros(1)
        self.momentum = 0.001      # EMA convergence ~ 1 000 samples
        self.clip_std = 5.0        # outlier threshold

    # ── Extraction ─────────────────────────────────────────────────────

    @staticmethod
    def _safe_float(val, default: float = 0.0) -> float:
        if val is None:
            return default
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except (ValueError, TypeError):
                return default
        if isinstance(val, bool):
            return 1.0 if val else 0.0
        return default

    def extract_features(self, snapshot: dict) -> np.ndarray:
        """Extract raw (non-normalised) feature vector from snapshot.

        Returns ndarray of shape (N_FEATURES,).
        """
        feats = []

        # Numeric features
        for key in NUMERIC_FEATURES:
            val = self._safe_float(snapshot.get(key, 0))

            # Log-transform skewed magnitudes
            if key in ('volume', 'buy_volume', 'sell_volume', 'avg_volume',
                       'wall_bid_size', 'wall_ask_size', 'tick_speed',
                       'spread_velocity'):
                val = math.copysign(math.log1p(abs(val)), val) if val != 0 else 0.0

            # Map percentages → [0, 1]
            if key in ('bb_position',):
                val = val / 100.0
            if key in ('confidence', 'directional_probability'):
                val = val / 100.0

            # Clip ratio extremes
            if key in ('ba_ratio',):
                val = max(0.01, min(10.0, val))

            feats.append(val)

        # Categorical features
        for key in CATEGORICAL_FEATURES:
            raw = snapshot.get(key, '')
            mapping = CATEGORICAL_MAP.get(key, {})
            feats.append(mapping.get(raw, 0.0))

        return np.array(feats, dtype=np.float32)

    # ── Online normalization ───────────────────────────────────────────

    def update_stats(self, features: np.ndarray):
        f = torch.from_numpy(features)
        n = self.running_count.item()

        if n == 0:
            self.running_mean.copy_(f)
            self.running_std.fill_(1.0)
            self.running_count.fill_(1.0)
        else:
            self.running_mean = (self.running_mean * (1 - self.momentum)
                                 + f * self.momentum)
            var = torch.mean((f - self.running_mean) ** 2)
            self.running_std = (self.running_std * (1 - self.momentum)
                                + torch.sqrt(var + 1e-8) * self.momentum)
            self.running_count.add_(1.0)

    def normalize(self, features: np.ndarray) -> np.ndarray:
        f = torch.from_numpy(features)
        std = self.running_std.clamp(min=1e-6)
        normed = (f - self.running_mean) / std
        normed = normed.clamp(-self.clip_std, self.clip_std)
        return normed.numpy()

    # ── Full pipeline ──────────────────────────────────────────────────

    def tensorize(self, snapshot: dict,
                  device: torch.device = None) -> torch.Tensor:
        raw = self.extract_features(snapshot)
        normed = self.normalize(raw)
        t = torch.from_numpy(normed)
        if device is not None:
            t = t.to(device)
        return t

    # ── Lightweight text embedding (hashing trick) ────────────────────

    # Trading keywords → feature dimension boosts for semantic relevance
    FEATURE_KEYWORD_BOOST: dict[str, list[str]] = {
        'rsi':       ['rsi', 'sobrecompra', 'overbought', 'sobreventa', 'oversold'],
        'macd':      ['macd', 'macd signal', 'macd hist', 'macd histogram'],
        'delta':     ['delta', 'agresión', 'aggression', 'flujo'],
        'cvd':       ['cvd', 'cumulative delta'],
        'volume':    ['volumen', 'volume', 'vol'],
        'buy_volume':   ['compra', 'buy volume', 'presión compradora'],
        'sell_volume':  ['venta', 'sell volume', 'presión vendedora'],
        'atr':       ['atr', 'volatilidad', 'volatility', 'rango'],
        'ema_20':    ['ema 20', 'ema20', 'media rápida'],
        'ema_50':    ['ema 50', 'ema50', 'media lenta'],
        'vwap':      ['vwap', 'precio promedio'],
        'ba_ratio':  ['bid ask', 'b/a', 'ba ratio', 'spread'],
        'imbalance': ['desequilibrio', 'imbalance'],
        'bb_position': ['bandas', 'bollinger', 'bb'],
        'trend':     ['tendencia', 'trend', 'alcista', 'bajista'],
        'trap_status': ['trampa', 'trap', 'liquidez', 'liquidation'],
        'wall_bid_size': ['muro', 'wall bid', 'soporte'],
        'wall_ask_size': ['muro', 'wall ask', 'resistencia'],
        'confidence':   ['confianza', 'confidence', 'convicción'],
    }

    def embed_text_block(self, text: str) -> torch.Tensor:
        """Lightweight hashing-trick encoder: text → 53-dim semantic vector.

        Uses double hash + trading keyword boosting so cosine similarity
        against a raw feature vector captures genuine semantic overlap.
        """
        cleaned = re.sub(r'[*_#`\[\]()>|\\]', ' ', text.lower())
        tokens = re.findall(r'[a-zA-Záéíóúñü]+', cleaned)

        vec = torch.zeros(N_FEATURES, dtype=torch.float32)

        # Primary hash: map each token to a feature index
        for token in tokens:
            idx = abs(hash(token)) % N_FEATURES
            vec[idx] += 1.0

        # Keyword boost: trading terms → relevant feature indices
        for feat_name, keywords in self.FEATURE_KEYWORD_BOOST.items():
            try:
                idx = NUMERIC_FEATURES.index(feat_name)
            except ValueError:
                try:
                    idx = len(NUMERIC_FEATURES) + CATEGORICAL_FEATURES.index(feat_name)
                except ValueError:
                    continue
            count = sum(1 for kw in keywords if kw in cleaned)
            if count > 0:
                vec[idx] += float(count) * 2.0

        # L2 normalize → unit vector (cosine-similarity ready)
        norm = vec.norm()
        if norm > 0:
            vec = vec / norm

        return vec


# ══════════════════════════════════════════════════════════════════════════════
# 3. BRAIN AGENT
# ══════════════════════════════════════════════════════════════════════════════

class BrainAgent:
    """Central AI controller — inference, state, alert dispatch.

    Usage
    -----
        agent = BrainAgent(telegram_queue=bot._queue)
        agent.load_or_init()
        decision = await agent.evaluate_market_state(snapshot)
        # decision → {direction, confidence_pct, market_rationale, risk_bracket, ...}
    """

    def __init__(self,
                 telegram_queue: Optional[Queue] = None,
                 device: str = 'auto'):
        self.device = self._resolve_device(device)
        self.model = QuantumBrainNetwork().to(self.device)
        self.pipeline = FeaturePipeline()
        self.telegram_queue = telegram_queue

        # Persistent LSTM state
        self._hidden_state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None

        # Smoothing buffer
        self._pred_buffer: deque = deque(maxlen=SMOOTHING_WINDOW)

        # Hysteresis state — track previous direction for flip guard
        self._prev_hysteresis_dir: str = 'INCIERTO'

        # Low-confidence flip cooldown (50–60 % → 180 s block)
        self._last_opposite_signal_time: float = 0.0

        # Cooldown
        self._last_alert_time: float = 0.0
        self._last_direction: str = 'INCIERTO'

        # Stats
        self._inference_latency: float = 0.0
        self._total_calls: int = 0

        # ── Knowledge injection ─────────────────────────────────────────
        self._knowledge_index = KnowledgeIndex()
        self._brain_knowledge_blocks: list[str] = []
        self._last_matched_blocks: list[tuple[str, float]] = []

        # ── Training infrastructure ─────────────────────────────────────
        self._optimizer: Optional[torch.optim.Optimizer] = None
        self._last_retrain_time: float = 0.0
        self._train_losses: deque = deque(maxlen=100)
        self._train_accuracy: deque = deque(maxlen=100)
        self._best_val_loss: float = float('inf')

        self.model.eval()

    # ── Public API ─────────────────────────────────────────────────────

    def tensorize_snapshot(self, snapshot: dict) -> torch.Tensor:
        """Clean → normalise → torch.Tensor (non-blocking, no model call)."""
        return self.pipeline.tensorize(snapshot, self.device)

    def infer_sync(self, snapshot: dict,
                   knowledge_blocks: Optional[list[str]] = None,
                   temperature: float = 0.5) -> dict:
        """Synchronous inference — call from QTimer / UI thread.

        Parameters
        ----------
        snapshot : dict
            Current market snapshot with indicator fields.
        knowledge_blocks : list[str] | None
            Parsed .md blocks from F3 tab. If None, falls back to
            ``self._brain_knowledge_blocks``.
        temperature : float
            Knowledge bias strength (0.0 = ignore blocks, 1.0 = full weight).

        Returns brain_decision dict (same shape as evaluate_market_state)
        without dispatching Telegram alerts. Use this inside refresh_data().
        """
        brain = self._empty_decision(snapshot)

        try:
            # ── Anti-latency: validate snapshot freshness ─────────────────
            snap_time = snapshot.get('_snapshot_time', None)
            if snap_time is not None:
                age_ms = (time.time() - snap_time) * 1000
                if age_ms > 500:
                    log.warning(
                        f'[BrainAgent] Snapshot obsoleto por {age_ms:.0f}ms '
                        f'(> 500ms) — inferencia invalidada'
                    )
                    brain['market_rationale'] = f'Snapshot obsoleto ({age_ms:.0f}ms)'
                    return brain

            start = time.perf_counter()

            raw = self.pipeline.extract_features(snapshot)
            self.pipeline.update_stats(raw)
            x = self.pipeline.tensorize(snapshot, self.device)

            # ── Forward pass ────────────────────────────────────────────
            with torch.no_grad():
                log_probs, self._hidden_state = self.model(x, self._hidden_state)

            # ── Knowledge injection into log_probs ──────────────────────
            kb = knowledge_blocks if knowledge_blocks is not None else self._brain_knowledge_blocks
            self._last_matched_blocks.clear()

            if kb and temperature > 0.0:
                try:
                    raw_tensor = torch.from_numpy(raw)
                    # Use KnowledgeIndex (precomputed embedding matrix) for O(1) search
                    use_index = (knowledge_blocks is None
                                 and self._knowledge_index.stats()['indexed'])

                    if use_index:
                        top_results = self._knowledge_index.search(
                            raw_tensor, top_k=3
                        )
                        self._last_matched_blocks = top_results
                        top_blocks = [b for b, _ in top_results]
                    else:
                        # Fallback for ad-hoc blocks not loaded into index
                        embeds = torch.stack([
                            self.pipeline.embed_text_block(b) for b in kb
                        ])
                        sims = F.cosine_similarity(
                            raw_tensor.unsqueeze(0), embeds
                        )
                        k = min(3, len(kb))
                        top_idx = sims.topk(k).indices.tolist()
                        top_blocks = [kb[i] for i in top_idx]
                        top_sims = [sims[i].item() for i in top_idx]
                        self._last_matched_blocks = list(zip(top_blocks, top_sims))

                    # Compute directional bias
                    k_bias = self._compute_knowledge_bias(top_blocks, temperature)
                    k_bias = k_bias.to(self.device)

                    # Inject into log_probs (log-space addition)
                    log_probs = log_probs + k_bias
                except Exception:
                    log.exception('[BrainAgent] knowledge injection failed')

            # ── Episodic memory injection ─────────────────────────
            try:
                similar = self.get_episodic_context(snapshot, k=3)
                if similar:
                    ep_bias = torch.tensor([0.0, 0.0, 0.0], device=self.device)
                    for item in similar:
                        rec = item['record']
                        sim = item['similarity']
                        weight = sim * 0.3 * temperature
                        if rec.label == 'FAILED':
                            ep_bias[1] += weight  # bias away from FAILED direction
                            ep_bias[2] += weight * 0.5  # increase uncertainty
                        elif rec.label == 'SUCCESS':
                            if rec.direction == 'ALZA':
                                ep_bias[0] += weight
                            elif rec.direction == 'BAJA':
                                ep_bias[1] += weight
                    log_probs = log_probs + ep_bias
            except Exception:
                pass

            # Final re-normalise
            log_probs = F.log_softmax(log_probs, dim=-1)

            probs = torch.exp(log_probs).cpu().numpy()

            elapsed = (time.perf_counter() - start) * 1000
            brain['inference_latency_ms'] = round(elapsed, 1)

            if np.any(np.isnan(probs)):
                log.warning('[BrainAgent] NaN in output — resetting hidden state')
                self.reset_hidden_state()
                return brain

            p_alza, p_baja, p_incierto = float(probs[0]), float(probs[1]), float(probs[2])

            # ── Absolute Uncertainty Filter ────────────────────────────────────
            if p_incierto > 0.40:
                brain['direction'] = 'INCIERTO'
                brain['confidence_pct'] = round(p_incierto * 100, 1)
                brain['prob_alza'] = round(p_alza * 100, 1)
                brain['prob_baja'] = round(p_baja * 100, 1)
                brain['prob_incierto'] = round(p_incierto * 100, 1)
                self._total_calls += 1
                log.info(
                    f'[BrainAgent] INCERTIDUMBRE > 40% '
                    f'({p_incierto*100:.1f}%) — veredicto bloqueado'
                )
                return brain

            self._pred_buffer.append((p_alza, p_baja, p_incierto))
            avg = np.mean(self._pred_buffer, axis=0)
            p_alza_s, p_baja_s, p_incierto_s = (float(avg[0]), float(avg[1]), float(avg[2]))

            raw_direction: str
            if p_alza_s >= p_baja_s and p_alza_s >= p_incierto_s:
                raw_direction = 'ALZA'; raw_confidence = p_alza_s
            elif p_baja_s >= p_alza_s and p_baja_s >= p_incierto_s:
                raw_direction = 'BAJA'; raw_confidence = p_baja_s
            else:
                raw_direction = 'INCIERTO'; raw_confidence = p_incierto_s

            # ── Volatility Explosion: bypass all passive filters ──────────────
            # When tick_speed > 3× 5-min avg or order-book z-score > 3.5σ, the
            # system interprets this as institutional presence and removes
            # hysteresis, exhaustion, and cooldown guards.
            volatility_explosion = snapshot.get('volatility_explosion', False)

            direction = raw_direction
            confidence = raw_confidence
            flip_blocked = False

            if not volatility_explosion:
                # ── Directional Hysteresis Filter (skipped during explosion) ──
                if raw_direction != 'INCIERTO' and self._prev_hysteresis_dir != 'INCIERTO':
                    is_flip = (
                        (self._prev_hysteresis_dir == 'ALZA' and raw_direction == 'BAJA') or
                        (self._prev_hysteresis_dir == 'BAJA' and raw_direction == 'ALZA')
                    )
                    if is_flip:
                        delta_alza_baja = p_alza_s - p_baja_s
                        abs_delta = abs(delta_alza_baja)

                        if abs_delta < HYSTERESIS_DELTA:
                            direction = self._prev_hysteresis_dir
                            confidence = max(p_alza_s, p_baja_s)
                            flip_blocked = True
                            log.info(
                                f'[BrainAgent] Hysteresis bloqueó giro a {raw_direction} '
                                f'(Δ={abs_delta:.3f} < {HYSTERESIS_DELTA}) → mantiene {direction}'
                            )

                        # ── Exhaustion Trigger (skipped during explosion) ────────
                        if not flip_blocked and direction != 'INCIERTO':
                            if not self.check_market_exhaustion(snapshot, direction):
                                direction = self._prev_hysteresis_dir
                                confidence = max(p_alza_s, p_baja_s)
                                flip_blocked = True
                                log.info(
                                    f'[BrainAgent] Exhaustion bloqueó giro a {raw_direction} '
                                    f'— sin agotamiento microestructural'
                                )

                # ── Low-Confidence Flip Cooldown (skipped during explosion) ──
                if not flip_blocked and direction != 'INCIERTO' and self._prev_hysteresis_dir != 'INCIERTO':
                    is_flip = (
                        (self._prev_hysteresis_dir == 'ALZA' and direction == 'BAJA') or
                        (self._prev_hysteresis_dir == 'BAJA' and direction == 'ALZA')
                    )
                    if is_flip and 0.50 <= confidence < 0.60:
                        now = time.time()
                        since_last = now - self._last_opposite_signal_time
                        if since_last < LOW_CONF_COOLDOWN:
                            direction = self._prev_hysteresis_dir
                            flip_blocked = True
                            log.info(
                                f'[BrainAgent] Low-conf cooldown bloqueó giro — '
                                f'{since_last:.0f}s < {LOW_CONF_COOLDOWN}s'
                            )
                        else:
                            self._last_opposite_signal_time = now

            # Update hysteresis state (always, to keep reference)
            self._prev_hysteresis_dir = direction

            # Final INCIERTO override
            if direction not in ('ALZA', 'BAJA'):
                direction = 'INCIERTO'
                confidence = p_incierto_s

            brain['direction'] = direction
            brain['confidence_pct'] = round(confidence * 100, 1)
            brain['prob_alza'] = round(p_alza_s * 100, 1)
            brain['prob_baja'] = round(p_baja_s * 100, 1)
            brain['prob_incierto'] = round(p_incierto_s * 100, 1)
            brain['flip_blocked'] = flip_blocked

            self._total_calls += 1
            self._inference_latency = elapsed

            if direction != 'INCIERTO' and confidence > 0.6:
                brain['market_rationale'] = self._generate_rationale(snapshot, raw, direction)

            # ── Risk bracket (SL/TP NUNCA pueden ser 0) ─────────────────
            if direction in ('ALZA', 'BAJA'):
                price = snapshot.get('price', 0)
                atr = snapshot.get('atr', 0)
                # Fallback a Bollinger Bands si ATR es 0
                if atr is None or atr <= 0:
                    bb_upper = snapshot.get('bb_upper', 0)
                    bb_lower = snapshot.get('bb_lower', 0)
                    atr = (bb_upper - bb_lower) / 4 if (bb_upper - bb_lower) > 0 else price * 0.005
                    if atr <= 0:
                        atr = price * 0.005  # safety: 0.5% del precio

                if volatility_explosion:
                    sl_mult = 1.0
                    tp1_mult = 3.0
                    tp2_mult = 4.0
                    risk_capital = 20.0
                else:
                    sl_mult = 1.5
                    tp1_mult = 2.0          # ratio 1:2 (SL → TP1)
                    tp2_mult = 3.0          # ratio 1:3 (SL → TP2)
                    risk_capital = 10.0

                sl_dist = atr * sl_mult

                if direction == 'ALZA':
                    sl = price - sl_dist
                    tp1 = price + sl_dist * tp1_mult
                    tp2 = price + sl_dist * tp2_mult
                else:
                    sl = price + sl_dist
                    tp1 = price - sl_dist * tp1_mult
                    tp2 = price - sl_dist * tp2_mult

                # Safety: si por cualquier razón SL/TP son <= 0, usar % del precio
                if sl <= 0 or tp1 <= 0:
                    sl_pct = price * 0.015
                    sl = price - sl_pct if direction == 'ALZA' else price + sl_pct
                    tp1 = price + sl_pct * 1.5 if direction == 'ALZA' else price - sl_pct * 1.5
                    tp2 = price + sl_pct * 2.0 if direction == 'ALZA' else price - sl_pct * 2.0

                brain['risk_bracket'] = {
                    'status': 'LONG' if direction == 'ALZA' else 'SHORT',
                    'trigger': price,
                    'sl': sl,
                    'tp1': tp1,
                    'tp2': tp2,
                    'lot_size': risk_capital / sl_dist if sl_dist > 0 else 0,
                    'atr_used': round(atr, 2),
                    'sl_dist': round(sl_dist, 2),
                }
            # ── Store episodic memory ──────────────────────────────────
            try:
                if direction in ('ALZA', 'BAJA') and confidence > 0.5:
                    snap_with_dir = dict(snapshot)
                    snap_with_dir['brain_direction'] = direction
                    snap_with_dir['brain_confidence_pct'] = confidence * 100
                    self.store_episodic(snap_with_dir)
            except Exception:
                pass

        except torch.cuda.OutOfMemoryError:
            log.error('[BrainAgent] CUDA OOM — falling back to CPU')
            self.device = torch.device('cpu')
            self.model = self.model.to(self.device)
        except Exception:
            log.exception('[BrainAgent] infer_sync failed')
            brain['direction'] = 'INCIERTO'
            brain['confidence_pct'] = 0.0
            brain['market_rationale'] = 'Error de inferencia'

        return brain

    async def evaluate_market_state(self, snapshot: dict,
                                     knowledge_blocks: Optional[list[str]] = None,
                                     temperature: float = 0.5) -> dict:
        """Async inference entry-point (for non-blocking external callers).

        Delegates to infer_sync() then dispatches Telegram alert if qualified.

        Parameters
        ----------
        snapshot : dict
            Current market snapshot.
        knowledge_blocks : list[str] | None
            Parsed knowledge blocks for semantic injection.
        temperature : float
            Knowledge bias strength (0.0–1.0).
        """
        brain = self.infer_sync(snapshot,
                                knowledge_blocks=knowledge_blocks,
                                temperature=temperature)
        if brain['direction'] != 'INCIERTO':
            await self._dispatch_if_qualified(brain, snapshot)
        return brain

    def _empty_decision(self, snapshot: dict) -> dict:
        return {
            'direction': 'INCIERTO', 'confidence_pct': 0.0,
            'market_rationale': '',
            'risk_bracket': {'status': 'WAITING', 'trigger': 0.0, 'sl': 0.0,
                             'tp1': 0.0, 'tp2': 0.0, 'lot_size': 0.0},
            'prob_alza': 0.0, 'prob_baja': 0.0, 'prob_incierto': 0.0,
            'inference_latency_ms': 0.0,
            'timestamp': snapshot.get('timestamp', ''),
            'flip_blocked': False,
        }

    def reset_hidden_state(self):
        """Reset LSTM hidden state (after NaN or regime change)."""
        self._hidden_state = None
        self._pred_buffer.clear()
        self._prev_hysteresis_dir = 'INCIERTO'
        self._last_opposite_signal_time = 0.0

    def get_stats(self) -> dict:
        try:
            mem_stats = get_episodic_memory().stats()
        except Exception:
            mem_stats = {}
        return {
            'total_calls': self._total_calls,
            'avg_latency_ms': round(self._inference_latency, 1),
            'last_direction': self._last_direction,
            'device': str(self.device),
            'buffer_size': len(self._pred_buffer),
            'knowledge_blocks': len(self._brain_knowledge_blocks),
            'knowledge_index': self._knowledge_index.stats(),
            'hysteresis_dir': self._prev_hysteresis_dir,
            'last_opposite_elapsed': round(time.time() - self._last_opposite_signal_time, 1) if self._last_opposite_signal_time > 0 else 0.0,
            **self.get_training_stats(),
            'episodic_memory': mem_stats,
        }

    # ── Knowledge injection API ───────────────────────────────────────

    def set_knowledge_blocks(self, blocks: list[str]):
        """Receive parsed knowledge blocks from F3 UI and precompute embedding matrix.

        Uses KnowledgeIndex for O(1) search at inference time.
        """
        self._brain_knowledge_blocks = list(blocks)
        self._last_matched_blocks.clear()
        self._knowledge_index.load(blocks)
        log.info('[BrainAgent] Knowledge blocks updated — '
                 f'{len(blocks)} reglas asimiladas '
                 f'(index precomputado {self._knowledge_index.stats()["size_kb"]} KB)')

    @staticmethod
    def _compute_knowledge_bias(block_texts: list[str],
                                 temperature: float) -> torch.Tensor:
        """Analyse top-matched blocks for directional keywords → (3,) bias tensor.

        Returns [bias_alza, bias_baja, bias_incierto] scaled by temperature.
        """
        bias = torch.tensor([0.0, 0.0, 0.0])

        LONG_KW = ['long', 'compra', 'alza', 'bull', 'comprar',
                   'acumulación', 'acumulacion', 'absorción bid',
                   'cobertura larga', 'largo']
        SHORT_KW = ['short', 'venta', 'baja', 'bear', 'vender',
                    'distribución', 'distribucion', 'absorción ask',
                    'cobertura corta', 'corto']

        for text in block_texts:
            t = text.lower()
            long_score = sum(1 for kw in LONG_KW if kw in t)
            short_score = sum(1 for kw in SHORT_KW if kw in t)

            net = long_score - short_score
            if net > 0:
                bias[0] += float(net)       # ALZA
            elif net < 0:
                bias[1] += float(abs(net))  # BAJA

        # Normalise magnitude and apply temperature
        mag = bias.norm().item()
        if mag > 0:
            bias = bias / mag * min(mag, 3.0)
        return bias * temperature

    # ── Model persistence ──────────────────────────────────────────────

    def load_or_init(self, path: Optional[str] = None):
        """Load pretrained weights or initialise fresh model."""
        if path is None:
            path = os.path.join(os.path.dirname(__file__),
                                '..', '..', 'models', 'quantum_brain.pth')
        if os.path.exists(path):
            try:
                state = torch.load(path, map_location=self.device)
                self.model.load_state_dict(state['model'])
                self.pipeline.running_mean = state.get('running_mean',
                                                        self.pipeline.running_mean)
                self.pipeline.running_std = state.get('running_std',
                                                       self.pipeline.running_std)
                self.pipeline.running_count = state.get('running_count',
                                                         self.pipeline.running_count)
                log.info(f'[BrainAgent] Model loaded from {path}')
            except Exception as e:
                log.warning(f'[BrainAgent] Failed to load {path}: {e}')
        else:
            log.info('[BrainAgent] No saved model found — fresh init')

    def save(self, path: Optional[str] = None):
        """Save model weights + normalisation stats."""
        if path is None:
            path = os.path.join(os.path.dirname(__file__),
                                '..', '..', 'models', 'quantum_brain.pth')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = {
            'model': self.model.state_dict(),
            'running_mean': self.pipeline.running_mean,
            'running_std': self.pipeline.running_std,
            'running_count': self.pipeline.running_count,
        }
        torch.save(state, path)
        log.info(f'[BrainAgent] Model saved to {path}')

    # ── Episodic memory integration ────────────────────────────────────

    def store_episodic(self, snapshot: dict) -> bool:
        """Store critical event in episodic memory (if triggered)."""
        try:
            mem = get_episodic_memory()
            rec = mem.store(snapshot)
            return rec is not None
        except Exception:
            return False

    def get_episodic_context(self, snapshot: dict,
                              k: int = 3) -> List[dict]:
        """Retrieve top-k similar past episodes for context injection."""
        try:
            mem = get_episodic_memory()
            return mem.search_from_snapshot(snapshot, k=k, min_sim=0.12)
        except Exception:
            return []

    def label_episodic_outcome(self, label: str):
        """Label the most recent memory record (SUCCESS / FAILED)."""
        try:
            mem = get_episodic_memory()
            mem.label_last(label)
        except Exception:
            pass

    # ── Asymmetric loss function ───────────────────────────────────────

    @staticmethod
    def _trauma_weighted_loss(log_probs: torch.Tensor,
                               target: torch.Tensor,
                               labels: List[str]) -> torch.Tensor:
        """Custom loss with 3x penalty for FAILED (trauma) patterns.

        Parameters
        ----------
        log_probs : (B, 3) — log-softmax output.
        target   : (B,) — class indices [0=ALZA, 1=BAJA, 2=INCIERTO].
        labels   : list[str] — per-sample 'SUCCESS' / 'FAILED' / 'PENDING'.

        Returns
        -------
        Scalar loss tensor.
        """
        base_loss = F.nll_loss(log_probs, target, reduction='none')
        weights = torch.ones_like(base_loss)
        for i, lbl in enumerate(labels):
            if lbl == 'FAILED':
                weights[i] = TRAUMA_LOSS_MULTIPLIER
        return (base_loss * weights).mean()

    # ── Background retraining ──────────────────────────────────────────

    async def background_retrain(self):
        """Asynchronous retraining loop — runs every RETRAIN_INTERVAL.

        Extracts mini-batches from episodic memory, applies asymmetric
        trauma-weighted loss, saves improved model checkpoints.
        """
        try:
            mem = get_episodic_memory()
        except Exception:
            log.warning("[BrainAgent] Episodic memory no disponible — "
                        "retrain omitido")
            return

        now = time.time()
        elapsed = now - self._last_retrain_time
        if elapsed < RETRAIN_INTERVAL and self._last_retrain_time > 0:
            return

        self._last_retrain_time = now
        log.info("[BrainAgent] Iniciando retrain background...")

        batch = mem.get_training_batch(max_samples=MAX_TRAIN_SAMPLES)
        if len(batch) < MIN_TRAIN_SAMPLES:
            log.info("[BrainAgent] Muestras insuficientes (%d < %d) — "
                     "retrain pospuesto", len(batch), MIN_TRAIN_SAMPLES)
            return

        # Build dataset
        failed_count = sum(1 for r in batch if r.label == 'FAILED')
        success_count = sum(1 for r in batch if r.label == 'SUCCESS')
        log.info("[BrainAgent] Retrain con %d muestras "
                 "(%d FAILED × %dx + %d SUCCESS)",
                 len(batch), failed_count // 3, TRAUMA_LOSS_MULTIPLIER,
                 success_count)

        features_list, targets_list, labels_list = [], [], []
        for rec in batch:
            # Reconstruct approximate feature vector from record fields
            dummy_snapshot = {
                'price': rec.price,
                'delta': rec.delta,
                'rsi': rec.rsi,
                'bb_position': rec.bb_position,
                'atr': rec.atr,
                'trap_status': rec.trap_status,
                'brain_direction': rec.direction,
                'brain_confidence_pct': rec.confidence,
                # Fill required features with reasonable defaults
                'change_pct': 0, 'vwap': 0, 'vwap_dist': 0,
                'day_high': rec.price * 1.01,
                'day_low': rec.price * 0.99,
                'macd': 0, 'macd_signal': 0, 'macd_hist': 0,
                'ema_20': rec.price * 0.99, 'ema_50': rec.price * 0.98,
                'delta_accel': 0, 'cvd': 0,
                'buy_volume': 0, 'sell_volume': 0, 'volume': 0,
                'avg_volume': 0, 'ba_ratio': 1.0, 'imbalance': 0,
                'depth_imb_pct': 0, 'cumulative_delta': 0,
                'kaufman_eff': 0.5, 'spread_velocity': 0,
                'tick_speed': 0, 'cancel_rate': 0,
                'skewness': 0, 'pinam': 0,
                'rsi_5m': rec.rsi, 'rsi_15m': rec.rsi,
                'confluence_score': 50,
                'wall_bid_size': 0, 'wall_ask_size': 0, 'liq_zones': 0,
                'directional_probability': rec.confidence,
                'confidence': rec.confidence,
                # Categorical defaults
                'trend': 'NEUTRAL', 'signal_text': 'WAIT',
                'bb_squeeze': 'NORMAL', 'force': 'NONE',
                'trend_5m': 'WAIT', 'trend_15m': 'WAIT',
                'trend_1h': 'WAIT', 'trend_4h': 'WAIT',
                'market_bias': 'INCIERTO',
                'ema_cross_5m': 'NEUTRAL', 'ema_cross_15m': 'NEUTRAL',
            }
            feats = self.pipeline.extract_features(dummy_snapshot)
            target_idx = 0 if rec.direction == 'ALZA' else (
                1 if rec.direction == 'BAJA' else 2
            )
            features_list.append(feats)
            targets_list.append(target_idx)
            labels_list.append(rec.label)

        features_t = torch.tensor(np.array(features_list),
                                   dtype=torch.float32)
        targets_t = torch.tensor(targets_list, dtype=torch.long)

        # Split train/val
        split = int(len(features_t) * 0.8)
        train_feats, val_feats = features_t[:split], features_t[split:]
        train_targets, val_targets = targets_t[:split], targets_t[split:]
        train_labels = labels_list[:split]
        val_labels = labels_list[split:]

        # Init optimizer on first call
        if self._optimizer is None:
            self._optimizer = torch.optim.AdamW(
                self.model.parameters(),
                lr=LEARNING_RATE,
                weight_decay=WEIGHT_DECAY,
            )

        # Training loop
        self.model.train()
        epoch_losses = []
        for epoch in range(TRAIN_EPOCHS):
            self._optimizer.zero_grad()

            log_probs, _ = self.model(train_feats)
            loss = self._trauma_weighted_loss(
                log_probs, train_targets, train_labels
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self._optimizer.step()

            # Validation
            with torch.no_grad():
                val_log_probs, _ = self.model(val_feats)
                val_loss = self._trauma_weighted_loss(
                    val_log_probs, val_targets, val_labels
                )
                val_preds = val_log_probs.argmax(dim=1)
                val_acc = (val_preds == val_targets).float().mean().item()

            epoch_losses.append((loss.item(), val_loss.item(), val_acc))
            log.info("[BrainAgent] Retrain epoch %d/%d — "
                     "train_loss=%.4f val_loss=%.4f val_acc=%.2f%%",
                     epoch + 1, TRAIN_EPOCHS,
                     loss.item(), val_loss.item(), val_acc * 100)

        # Stats
        avg_train = sum(e[0] for e in epoch_losses) / len(epoch_losses)
        avg_val = sum(e[1] for e in epoch_losses) / len(epoch_losses)
        final_acc = epoch_losses[-1][2]
        self._train_losses.append(avg_train)
        self._train_accuracy.append(final_acc)

        log.info("[BrainAgent] Retrain completado — "
                 "avg_train=%.4f avg_val=%.4f final_acc=%.2f%% "
                 "(%d muestras, %d FAILED ponderados)",
                 avg_train, avg_val, final_acc * 100,
                 len(batch), failed_count)

        # Save if validation improved
        if avg_val < self._best_val_loss:
            self._best_val_loss = avg_val
            self.save()
            log.info("[BrainAgent] Nuevo checkpoint guardado "
                     "(val_loss=%.4f)", avg_val)

        self.model.eval()

    def get_training_stats(self) -> dict:
        return {
            'last_retrain': self._last_retrain_time,
            'total_retrain_calls': len(self._train_losses),
            'avg_loss': round(np.mean(self._train_losses), 4) if self._train_losses else 0,
            'avg_accuracy': round(np.mean(self._train_accuracy) * 100, 1) if self._train_accuracy else 0,
            'best_val_loss': round(self._best_val_loss, 4) if self._best_val_loss < float('inf') else 0,
            'optimizer_initialized': self._optimizer is not None,
        }

    # ── Exhaustion Trigger Engine ──────────────────────────────────────

    @staticmethod
    def check_market_exhaustion(snapshot: dict, proposed_direction: str) -> bool:
        """Validate that a counter-trend flip is backed by microstructural exhaustion.

        Parameters
        ----------
        snapshot : dict
            Current market snapshot with BB, CVD, Kaufman, tick_speed fields.
        proposed_direction : str
            Direction the model wants to emit ('ALZA' or 'BAJA').

        Returns
        -------
        bool
            True if exhaustion conditions confirm the counter-trend move.
        """
        bb_pos = snapshot.get('bb_position', 50.0)
        kaufman = snapshot.get('kaufman_eff', 1.0)
        tick_spd = snapshot.get('tick_speed', 0)
        cvd_val = snapshot.get('cvd', 0.0)
        prev_cvd = snapshot.get('prev_cvd', cvd_val)

        # Exhaustion regime: ranging / absorbing
        if kaufman > EXHAUSTION_KEFF_THRESHOLD:
            return False

        # Tick speed must be low (no directional thrust)
        if tick_spd > 20:
            return False

        if proposed_direction == 'BAJA':
            # Confirming a TOP exhaustion: price stretched above BB, CVD stalling
            if bb_pos < 75:
                return False
            cvd_slowing = abs(cvd_val) <= abs(prev_cvd) * 1.05 or cvd_val < prev_cvd
            if not cvd_slowing:
                return False
            return True

        if proposed_direction == 'ALZA':
            # Confirming a BOTTOM exhaustion: price stretched below BB, CVD rising
            if bb_pos > 25:
                return False
            cvd_rising = cvd_val > prev_cvd * 0.95
            if not cvd_rising:
                return False
            return True

        return False

    # ── Private ────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_device(device: str) -> torch.device:
        if device == 'auto':
            if torch.cuda.is_available():
                return torch.device('cuda:0')
            if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
                return torch.device('mps')
            return torch.device('cpu')
        return torch.device(device)

    def _generate_rationale(self, snapshot: dict, raw: np.ndarray,
                            direction: str) -> str:
        """Top-5 feature contributions via z-score deviation analysis."""
        contributions = []
        mean = self.pipeline.running_mean.numpy()
        std = self.pipeline.running_std.numpy().clip(1e-6)
        sign = 1.0 if direction == 'ALZA' else -1.0

        for i, key in enumerate(NUMERIC_FEATURES):
            if i >= len(raw):
                break
            if std[i] < 1e-6:
                continue
            z = (raw[i] - mean[i]) / std[i]
            contrib = z * sign
            if abs(contrib) > 1.5:
                contributions.append((key, contrib))

        contributions.sort(key=lambda x: -abs(x[1]))
        top = contributions[:5]

        if not top:
            return ('Análisis de Microestructura: Monitoreando fluctuación de '
                    'liquidez y velocidad de ticks en la zona de control.')

        labels = {
            'delta': 'delta agresión',
            'delta_accel': 'aceleración delta',
            'cvd': 'CVD',
            'rsi': 'RSI',
            'ba_ratio': 'ratio B/A',
            'imbalance': 'desequilibrio',
            'volume': 'volumen',
            'buy_volume': 'vol compra',
            'sell_volume': 'vol venta',
            'vwap': 'VWAP',
            'price_vwap_dist': 'dist VWAP',
            'kaufman_eff': 'eficiencia K',
            'tick_speed': 'tick speed',
            'cancel_rate': 'cancel rate',
            'pinam': 'PINAM',
            'skewness': 'asimetría',
            'spread_velocity': 'spread vel',
            'directional_probability': 'prob direccional',
            'confidence': 'confianza',
            'atr': 'ATR',
            'confluence_score': 'confluencia MTF',
            'cumulative_delta': 'Δ acum',
            'depth_imb_pct': 'deseq prof',
        }

        parts = []
        for key, contrib in top:
            label = labels.get(key, key.replace('_', ' '))
            emoji = '\U0001f7e2' if contrib > 0 else '\U0001f7e3'
            parts.append(f'{emoji} {label} ({contrib:+.1f}σ)')

        rationale = ' | '.join(parts)

        # Append F3 rule validation if a matched block >= 0.3 cosine sim
        trap_status = snapshot.get('trap_status', '')
        has_trap = trap_status and 'SIN TRAMPA' not in trap_status
        if has_trap and self._last_matched_blocks:
            high_sim = [(b, s) for b, s in self._last_matched_blocks if s >= 0.30]
            if high_sim:
                preview = high_sim[0][0][:80].replace('\n', ' ')
                rationale += (
                    ' | \U0001f9e0 Validado por Regla de Bitácora F3: '
                    f'"{preview}…"'
                )

        return rationale

    async def _dispatch_if_qualified(self, brain: dict, snapshot: dict):
        direction = brain['direction']
        confidence = brain['confidence_pct'] / 100.0

        if direction == 'INCIERTO' or confidence < ALERT_THRESHOLD:
            self._last_direction = direction
            log.info(f'[📡 TELEGRAM BOT] Alerta automática bloqueada: '
                     f'Confianza ({confidence*100:.0f}%) por debajo del umbral '
                     f'({ALERT_THRESHOLD*100:.0f}%).')
            return

        now = time.time()

        # Low-confidence cooldown: 50–60 % → 180 s block on opposite signal
        if self._last_direction != 'INCIERTO' and direction != self._last_direction:
            if 0.50 <= confidence < 0.60:
                remaining = LOW_CONF_COOLDOWN - (now - self._last_opposite_signal_time)
                if remaining > 0:
                    log.info(
                        f'[📡 TELEGRAM BOT] Low-conf cooldown bloquea giro a '
                        f'{direction} ({confidence*100:.0f}%) — '
                        f'faltan {remaining:.0f}s'
                    )
                    return
                self._last_opposite_signal_time = now

        # Standard same-direction cooldown
        remaining = COOLDOWN_SECONDS - (now - self._last_alert_time)
        if (direction == self._last_direction
                and (now - self._last_alert_time) < COOLDOWN_SECONDS):
            log.info(f'[📡 TELEGRAM BOT] Alerta automática bloqueada por '
                     f'Cooldown activo (Faltan {remaining:.0f} segundos).')
            return

        alert = {
            'type': 'brain_signal',
            'direction': direction,
            'confidence_pct': round(confidence * 100, 1),
            'prob_alza': brain['prob_alza'],
            'prob_baja': brain['prob_baja'],
            'rationale': brain['market_rationale'],
            'risk_bracket': brain.get('risk_bracket', {}),
            'price': snapshot.get('price', 0),
            'timestamp': snapshot.get('timestamp', ''),
        }

        if self.telegram_queue is not None:
            try:
                self.telegram_queue.put_nowait(alert)
            except Exception:
                pass

        self._last_alert_time = now
        self._last_direction = direction
        log.info(f'[BrainAgent] ALERTA {direction} @ {confidence*100:.0f}%')


# ══════════════════════════════════════════════════════════════════════════════
# 4. CONVENIENCE FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def create_brain_agent(telegram_queue: Optional[Queue] = None,
                       device: str = 'auto',
                       load_model: bool = True) -> BrainAgent:
    """Factory: create, optionally load weights, return ready BrainAgent."""
    agent = BrainAgent(telegram_queue=telegram_queue, device=device)
    if load_model:
        agent.load_or_init()
    return agent
