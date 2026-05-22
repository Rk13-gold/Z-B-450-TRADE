import asyncio
import json
from typing import Dict, Optional
import aiohttp
from config.settings import settings


class AIAnalyst:
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def analyze_sentiment(self, market_data: Dict) -> Dict:
        if not self.api_key:
            return {
                'sentiment': 'neutral',
                'confidence': 0.0,
                'summary': 'API Key no configurada'
            }

        prompt = self._build_sentiment_prompt(market_data)

        try:
            result = await self._call_gemini(prompt)
            return self._parse_sentiment_response(result)
        except Exception as e:
            print(f"❌ Error en análisis de sentimiento: {e}")
            return {'sentiment': 'neutral', 'confidence': 0.0, 'error': str(e)}

    def _build_sentiment_prompt(self, market_data: Dict) -> str:
        delta = market_data.get('delta', 0)
        delta_strength = market_data.get('delta_strength', 0)
        rsi = market_data.get('rsi', 0)
        price = market_data.get('price', 0)

        prompt = f"""Analiza el sentimiento del mercado para BTCUSDT con los siguientes datos:
        - Precio actual: {price}
        - Delta: {delta}
        - Fuerza del Delta: {delta_strength}
        - RSI: {rsi}

        Proporciona un resumen breve del sentimiento del mercado (alcista/bajista/neutral) y un nivel de confianza (0-1)."""

        return prompt

    async def _call_gemini(self, prompt: str) -> Dict:
        url = f"{self.base_url}/gemini-2.0-flash:generateContent?key={self.api_key}"

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"API Error: {response.status}")

    def _parse_sentiment_response(self, response: Dict) -> Dict:
        try:
            text = response.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')

            sentiment = 'neutral'
            confidence = 0.5

            text_lower = text.lower()
            if 'alcista' in text_lower or 'bullish' in text_lower or 'compra' in text_lower:
                sentiment = 'bullish'
                confidence = 0.7
            elif 'bajista' in text_lower or 'bearish' in text_lower or 'venta' in text_lower:
                sentiment = 'bearish'
                confidence = 0.7

            return {
                'sentiment': sentiment,
                'confidence': confidence,
                'summary': text[:200]
            }
        except Exception as e:
            return {
                'sentiment': 'neutral',
                'confidence': 0.0,
                'error': str(e)
            }

    async def post_trade_analysis(self, trade_data: Dict) -> Dict:
        if not self.api_key:
            return {'analysis': 'API Key no configurada'}

        prompt = self._build_post_trade_prompt(trade_data)

        try:
            result = await self._call_gemini(prompt)
            return self._parse_analysis_response(result)
        except Exception as e:
            return {'analysis': f'Error: {str(e)}'}

    def _build_post_trade_prompt(self, trade_data: Dict) -> str:
        pnl = trade_data.get('pnl', 0)
        entry = trade_data.get('entry_price', 0)
        exit_price = trade_data.get('exit_price', 0)
        side = trade_data.get('side', '')
        duration = trade_data.get('duration', 0)

        prompt = f"""Realiza un análisis post-trade conciso:
        - Dirección: {side}
        - Entrada: {entry}
        - Salida: {exit_price}
        - PnL: {pnl}
        - Duración: {duration} segundos

        Proporciona una breve evaluación de qué hice bien y qué podría mejorar."""

        return prompt

    def _parse_analysis_response(self, response: Dict) -> Dict:
        try:
            text = response.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            return {'analysis': text}
        except:
            return {'analysis': 'Error al procesar análisis'}


ai_analyst = AIAnalyst()