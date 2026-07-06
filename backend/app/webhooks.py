"""Discord / Telegram alert delivery. Fire-and-forget with short timeouts so a
dead webhook never blocks the API. Configure URLs/tokens in Settings page.
"""
from __future__ import annotations

import httpx


def format_alert(a: dict) -> str:
    return (
        f"EV ALERT  {a['match']}\n"
        f"{a['market']} / {a['selection']}"
        + (f" ({a['line']:+g})" if a.get("line") is not None else "") + "\n"
        f"Book: {a['sportsbook']} @ {a['book_american']:+d} (dec {a['book_decimal']:.2f})\n"
        f"Model p: {a['model_prob']:.3f}  |  Fair: {a['fair_decimal']:.2f}\n"
        f"EV: +{a['ev_pct']:.1f}%  |  Stake: flat {a.get('stake_flat', 0)} / kelly {a.get('stake_kelly', 0)}\n"
        f"Why: {a['reason']}"
    )


def send_discord(webhook_url: str, text: str) -> bool:
    if not webhook_url:
        return False
    try:
        r = httpx.post(webhook_url, json={"content": text[:1900]}, timeout=5.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    if not (bot_token and chat_id):
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]}, timeout=5.0)
        return r.status_code < 300
    except httpx.HTTPError:
        return False
