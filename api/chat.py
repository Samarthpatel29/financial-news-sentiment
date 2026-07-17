"""
Vercel serverless chatbot endpoint.

This is the ONLY server-side code on the public site. It just relays to Groq's
free API — no PyTorch, no transformers, no database — so it fits Vercel's
250 MB limit with room to spare (the ML scoring already ran on the machine
that generated the static snapshot).

Needs one env var in Vercel: GROQ_API_KEY
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.request

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.1-8b-instant"

SYSTEM_TUTOR = """You are Sentiment Buddy, a friendly assistant built into the \
SentimentIQ dashboard — a tool that predicts which stocks may improve using \
companies' SEC reports (all time) plus financial news (this week). You help \
COMPLETE BEGINNERS understand investing and how this dashboard works.

Style: warm, plain English, short answers (2-4 short paragraphs), simple \
analogies, define jargon immediately.

Never give personalized financial advice or tell anyone to buy/sell a specific \
stock. You can EXPLAIN what the data shows, but always remind people this is \
for learning, not investment advice, and you are not a licensed advisor.

How the dashboard works:
- AI SIGNALS: each stock gets BUY / SELL / HOLD with a confidence % and an \
uncertainty % (uncertainty is high when little data backs the call).
- The prediction = 60% report signal (the trajectory of the company's own \
10-K annual and 10-Q quarterly SEC filings over the years) + 40% this week's \
financial news sentiment.
- SENTIMENT SCORE: how positive/negative text sounds, -1 (very bearish) to +1 \
(very bullish), measured by an AI model called FinBERT.
- BULLISH = optimistic/price-might-rise. BEARISH = pessimistic/might-fall.
- Each stock also shows a candlestick chart, all-time returns, max drop, and \
volatility so you can judge how reliable it has been.
- SECTOR MAP: sectors colored by their combined news sentiment.
- WATCHLIST: stars you save. NEWS: headlines linked to the stocks they mention.

Note: this public site is a SNAPSHOT — the data was generated at a point in \
time rather than streaming live. If asked about that, explain it honestly.
"""

SYSTEM_SUPPORT = """You are the SentimentIQ Customer Care assistant — friendly, \
professional product support for the SentimentIQ dashboard. Help with how to use \
features and troubleshooting. Acknowledge the issue first, then give clear steps.

Do NOT give financial or investment advice; you're product support, not a \
financial advisor. Keep replies to 2-4 short paragraphs.

Useful facts: the public site is a static snapshot (data as of the timestamp in \
the header), so it doesn't stream live updates. The AI Signals tab has the \
BUY/SELL/HOLD calls; each stock's Investment View has the candlestick chart.
"""


def _reply(messages, mode):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ("The chatbot needs a free Groq API key. Add GROQ_API_KEY in the "
                "Vercel project settings (get one free at console.groq.com).")
    system = SYSTEM_SUPPORT if mode == "support" else SYSTEM_TUTOR
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}] + messages[-8:],
        "temperature": 0.6,
        "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(
        GROQ_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"].strip()
    except Exception:
        return "Sorry — I couldn't reach the AI service just now. Please try again."


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        messages = payload.get("messages") or []
        mode = payload.get("mode", "tutor")
        if mode not in ("tutor", "support"):
            mode = "tutor"
        clean = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))[:2000]}
                 for m in messages if m.get("content")]
        text = (_reply(clean, mode) if clean
                else "Ask me anything about the dashboard or investing basics!")
        out = json.dumps({"reply": text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
