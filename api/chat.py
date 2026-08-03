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
import sys
import time
import urllib.request
from collections import deque

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"

# ── Abuse limits ──────────────────────────────────────────────────────────────
# This endpoint relays to a free-tier Groq key with no auth in front of it, so
# anyone who finds the URL could drain the quota. These caps are per warm
# serverless instance (Vercel gives no shared state on the free plan) — not
# airtight, but they turn a trivial drain into a slow one.
RATE_WINDOW_SECONDS = 60
RATE_MAX_REQUESTS   = 12          # per client IP per window
MAX_MESSAGES        = 12
MAX_CHARS           = 2000
MAX_CONTEXT         = 16000       # dashboard snapshot the browser sends per turn

# Only these origins may call the endpoint from a browser. Set ALLOWED_ORIGIN in
# the Vercel project settings to your deployed domain.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")

_hits: dict[str, deque] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > RATE_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_MAX_REQUESTS:
        return True
    q.append(now)
    # keep the instance's memory bounded
    if len(_hits) > 512:
        for stale_ip in [k for k, v in _hits.items() if not v or now - v[-1] > 300]:
            _hits.pop(stale_ip, None)
    return False

SYSTEM_TUTOR = """You are Sentiment Buddy, a friendly assistant built into the \
SentimentIQ dashboard — a tool that predicts which stocks may improve by \
blending this week's financial news, the stock's price momentum, Wall-Street \
analyst consensus, and the company's SEC filings. You help COMPLETE BEGINNERS \
understand investing and how this dashboard works.

Style: talk like a friendly person texting, not an essay writer. KEEP IT SHORT: \
1 to 3 sentences by default. Only write more when the user explicitly asks you to \
"explain", "go deeper", or "compare". Never dump a long structured answer on a \
simple question. Do NOT use em-dashes (the long dash). Do NOT write dash-bullet \
or numbered lists unless the user asks for a list; answer in plain sentences. \
Warm, plain English, define jargon in a few words when it comes up. Vary how you \
open so you never sound like a template, and don't repeat yourself. Be \
interactive: answer, then when it helps, ask one short follow-up question to keep \
the conversation going, like a chat with a helpful friend.

Never tell anyone to buy or sell a specific stock, and never give personalized \
financial advice. You CAN explain what the data shows and why a rating came out \
the way it did. Mention that this is for learning and you're not a licensed \
advisor ONCE per conversation — the first time it's relevant — not in every \
message. Repeating the disclaimer every turn makes you useless.

How the dashboard works:
- AI SIGNALS: each stock gets BUY / SELL / HOLD with a confidence % and an \
uncertainty % (uncertainty is high when little data backs the call).
- The prediction blends four signals: 30% this week's financial-news sentiment, \
30% price momentum (whether the stock has actually been trending up), 25% \
Wall-Street analyst consensus, and 15% the trajectory of the company's own SEC \
filings (10-K annual / 10-Q quarterly). If some data is missing for a stock, the \
remaining parts are reweighted.
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
financial advisor. Match your length to the question — brief for simple ones, \
longer only when the steps genuinely require it.

Useful facts: the public site is a static snapshot (data as of the timestamp in \
the header), so it doesn't stream live updates. The AI Signals tab has the \
BUY/SELL/HOLD calls; each stock's Investment View has the candlestick chart.
"""


def _reply(messages, mode, context=""):
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return ("The chatbot needs a free Groq API key. Add GROQ_API_KEY in the "
                "Vercel project settings (get one free at console.groq.com).")
    system = SYSTEM_SUPPORT if mode == "support" else SYSTEM_TUTOR
    if context:
        # The browser sends a snapshot of exactly what the user is looking at, so
        # answers can quote the same numbers that are on screen.
        system += (
            "\n\nLIVE DASHBOARD DATA (what the user is looking at right now):\n"
            + context +
            "\n\nAnswer using these exact numbers and explain what they mean in "
            "plain English. The data includes a roster of EVERY stock on the "
            "dashboard (ticker, rating, confidence, sector, 1-year return, news "
            "mix) plus detailed blocks for any stock the user named — so you can "
            "answer 'which stocks are BUY?', 'list the tech stocks', or compare "
            "two tickers directly from the roster. If the user says \"this stock\" "
            "or \"it\", they mean the detailed stock above. If they ask something "
            "the data doesn't cover, say so plainly instead of inventing a figure. "
            "You may explain why a rating is what it is, but never tell them to "
            "buy or sell."
        )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}] + messages[-MAX_MESSAGES:],
        "temperature": 0.6,
        "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(
        GROQ_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 # Cloudflare fronts the Groq API and 403s (error 1010) the default
                 # "Python-urllib/3.x" agent, so identify ourselves explicitly.
                 "User-Agent": "SentimentIQ/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        # Surface the cause in the Vercel function log; keep the UI message kind.
        detail = getattr(exc, "read", lambda: b"")()[:300] or str(exc).encode()
        print(f"groq call failed: {type(exc).__name__}: {detail!r}", file=sys.stderr)
        return "Sorry — I couldn't reach the AI service just now. Please try again."


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, obj: dict) -> None:
        out = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(out)

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() if fwd else self.client_address[0]) or "?"

    def do_OPTIONS(self):
        self.send_response(204)
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        # Reject cross-site callers when an origin allow-list is configured.
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGIN and origin and origin != ALLOWED_ORIGIN:
            return self._send(403, {"reply": "This chatbot is not available from that site."})

        if _rate_limited(self._client_ip()):
            return self._send(429, {"reply": "You're sending messages very quickly — "
                                             "give it a few seconds and try again."})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 96_000:
                return self._send(413, {"reply": "That message is too long for me to read."})
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
        mode = payload.get("mode", "tutor")
        if mode not in ("tutor", "support"):
            mode = "tutor"
        clean = [
            {"role": "assistant" if m.get("role") == "assistant" else "user",
             "content": str(m.get("content", ""))[:MAX_CHARS]}
            for m in messages[-MAX_MESSAGES:]
            if isinstance(m, dict) and m.get("content")
        ]
        # Snapshot of the stock(s) the user is looking at, assembled in the browser
        context = str(payload.get("context", "") or "")[:MAX_CONTEXT]
        text = (_reply(clean, mode, context) if clean
                else "Ask me anything about the dashboard or investing basics!")
        self._send(200, {"reply": text})
