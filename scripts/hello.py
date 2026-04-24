"""Phase 0 smoke: ping Alpaca paper account + Anthropic.

Usage (from repo root, inside the agent container or a local venv):
    python scripts/hello.py

Prints paper-account equity and a short Anthropic response. Requires
ALPACA_API_KEY, ALPACA_API_SECRET, ANTHROPIC_API_KEY in .env.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# sys.path wiring for scripts run outside an installed package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _bootstrap  # noqa: F401  -- extends sys.path for lib/trading_platform/src

from dotenv import load_dotenv

load_dotenv()


def ping_alpaca() -> None:
    from alpaca.trading.client import TradingClient

    key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_API_SECRET"]
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    is_paper = "paper" in base
    client = TradingClient(key, secret, paper=is_paper)
    acct = client.get_account()
    print(f"[alpaca] paper={is_paper} equity={acct.equity} status={acct.status}")


def ping_anthropic() -> None:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.environ.get("ANTHROPIC_MODEL_JUDGE", "claude-haiku-4-5-20251001")
    resp = client.messages.create(
        model=model,
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with two words: hello ready"}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    print(f"[anthropic] model={model} response={text!r}")


if __name__ == "__main__":
    ping_alpaca()
    ping_anthropic()
