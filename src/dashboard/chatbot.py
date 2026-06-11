"""
Beginner-friendly chatbot for the SentimentIQ dashboard.

Uses Groq's free tier (llama-3.1-8b-instant) — the same free API key that
powers the AI Narrative. No extra cost, no extra key.

The bot is grounded with a live snapshot of the dashboard (market mood, top
tickers, top headlines) so it can answer questions like "why is NVDA bullish
today?" using the actual data on screen, and it can explain every concept in
the app for people who are new to investing.
"""
from __future__ import annotations
import logging

from config.settings import GROQ_API_KEY, CREW_LLM_MODEL

log = logging.getLogger(__name__)

# Strip the "groq/" prefix CrewAI uses — the raw SDK wants the bare model id
_MODEL = CREW_LLM_MODEL.split("/", 1)[-1]   # "llama-3.1-8b-instant"

_SYSTEM_PROMPT = """You are Sentiment Buddy, a friendly assistant built into the \
SentimentIQ dashboard — a real-time financial-news sentiment tool. You help \
COMPLETE BEGINNERS understand investing and how this dashboard works.

Your style:
- Warm, encouraging, plain English. Assume the person may know nothing about stocks.
- Short answers (2-4 short paragraphs max). Use simple analogies.
- Define jargon the moment you use it.
- Never give personalized financial advice or tell anyone to buy/sell a specific \
stock. You can EXPLAIN concepts and what the data shows, but always remind people \
that this is for learning, not investment advice, and you are not a licensed advisor.

How THIS dashboard works (explain when asked):
- It pulls financial news every ~60 seconds from free sources: Reuters, CNBC, \
MarketWatch, Dow Jones, Nasdaq, Benzinga, Yahoo Finance, FDA, SEC, Reddit, \
StockTwits, and more.
- SENTIMENT SCORE: how positive or negative a headline sounds, from -1 (very \
bearish/negative) to +1 (very bullish/positive). It's measured by an AI model \
called FinBERT (trained on financial text) with a backup called VADER.
- BULLISH = optimistic/price-might-rise mood. BEARISH = pessimistic/price-might-fall mood. \
NEUTRAL = no strong feeling.
- MESSAGE DENSITY: how much people are talking about something — more coverage = higher density.
- TRUST WEIGHT: institutional sources (Reuters, SEC, FDA) are Tier 1 and count more \
(1.0x) than aggregated sources (Tier 2, 0.75x).
- TIME DECAY: newer news matters more; old news fades in importance.
- RANK SCORE: combines all of the above (|sentiment| x density x trust x freshness) \
to sort which news matters most right now.
- TICKER: the short symbol for a company's stock, like AAPL for Apple or NVDA for Nvidia.
- The Ticker Heatmap ranks stocks by the combined sentiment of all their news.

If a question is off-topic from finance/the dashboard, gently steer back, but it's \
fine to answer simple general questions too.
"""


def _client():
    try:
        from groq import Groq
    except ImportError:
        return None
    if not GROQ_API_KEY or GROQ_API_KEY.startswith("PASTE_"):
        return None
    return Groq(api_key=GROQ_API_KEY)


def is_available() -> bool:
    return _client() is not None


def chat(messages: list[dict], context: str = "") -> str:
    """
    messages: [{"role": "user"|"assistant", "content": str}, ...] conversation history.
    context:  optional live dashboard snapshot to ground the answer.
    Returns the assistant's reply text.
    """
    client = _client()
    if client is None:
        return ("The chatbot needs a free Groq API key to work. Add GROQ_API_KEY "
                "to your .env file (get one free at console.groq.com) and restart.")

    system = _SYSTEM_PROMPT
    if context:
        system += f"\n\nLIVE DASHBOARD SNAPSHOT (use this for 'right now' questions):\n{context}"

    # Keep only the last 8 turns to stay fast and within free-tier limits
    convo = [{"role": "system", "content": system}] + messages[-8:]

    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=convo,
            temperature=0.6,
            max_tokens=500,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        log.warning("Chatbot error: %s", exc)
        return ("Sorry — I couldn't reach the AI service just now. "
                "Please try again in a moment.")
