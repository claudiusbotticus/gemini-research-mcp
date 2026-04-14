"""
Telegram Bot — Polymarket Market Monitor
Polls Polymarket for new/updated markets and posts updates to Telegram + Notion.
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from notion_client import AsyncClient as NotionClient
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

load_dotenv(Path(__file__).parent / ".env")

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NOTION_TOKEN     = os.environ["NOTION_TOKEN"]
NOTION_PAGE_ID   = os.environ["NOTION_PAGE_ID"]

POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default
STATE_FILE       = Path(__file__).parent / ".polymarket_state.json"

POLYMARKET_EVENTS_URL  = "https://gamma-api.polymarket.com/events"
POLYMARKET_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# ── Shutdown flag ────────────────────────────────────────────────────────────
_shutdown = asyncio.Event()

def _handle_signal(sig, frame):
    print(f"\n[{_ts()}] Signal {sig} received — shutting down gracefully...")
    _shutdown.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_event_ids": [], "seen_market_ids": [], "last_poll": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Polymarket API ───────────────────────────────────────────────────────────
async def fetch_events(session: aiohttp.ClientSession, limit: int = 20) -> list[dict]:
    params = {"limit": limit, "closed": "false", "order": "startDate", "ascending": "false"}
    async with session.get(POLYMARKET_EVENTS_URL, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_markets(session: aiohttp.ClientSession, limit: int = 20) -> list[dict]:
    params = {"limit": limit, "closed": "false", "order": "volume", "ascending": "false"}
    async with session.get(POLYMARKET_MARKETS_URL, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


# ── Telegram ─────────────────────────────────────────────────────────────────
def format_event_message(event: dict) -> str:
    title    = event.get("title", "Sin título")
    volume   = event.get("volume", 0)
    markets  = event.get("markets", [])
    n_mkts   = len(markets)
    slug     = event.get("slug", "")
    url      = f"https://polymarket.com/event/{slug}" if slug else "—"

    probs = []
    for m in markets[:3]:
        q   = m.get("question", "")
        p   = m.get("outcomePrices", "[]")
        try:
            prices = json.loads(p) if isinstance(p, str) else p
            yes_p  = float(prices[0]) * 100 if prices else 0
        except Exception:
            yes_p = 0
        probs.append(f"  • {q[:60]}: *{yes_p:.1f}%* YES")

    prob_lines = "\n".join(probs) if probs else "  —"
    vol_fmt    = f"${float(volume):,.0f}" if volume else "—"

    return (
        f"🆕 *Nuevo evento en Polymarket*\n"
        f"📌 *{title}*\n"
        f"💰 Volumen: {vol_fmt}\n"
        f"📊 Mercados ({n_mkts}):\n{prob_lines}\n"
        f"🔗 [Ver en Polymarket]({url})\n"
        f"🕐 _{_ts()}_"
    )


def format_market_message(market: dict) -> str:
    question = market.get("question", "Sin título")
    volume   = market.get("volume", 0)
    slug     = market.get("slug", "")
    url      = f"https://polymarket.com/market/{slug}" if slug else "—"

    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        yes_p  = float(prices[0]) * 100 if prices else 0
        no_p   = float(prices[1]) * 100 if len(prices) > 1 else 100 - yes_p
    except Exception:
        yes_p, no_p = 0, 0

    vol_fmt = f"${float(volume):,.0f}" if volume else "—"

    return (
        f"📈 *Nuevo mercado activo*\n"
        f"❓ *{question}*\n"
        f"✅ YES: *{yes_p:.1f}%* | ❌ NO: *{no_p:.1f}%*\n"
        f"💰 Volumen: {vol_fmt}\n"
        f"🔗 [Ver mercado]({url})\n"
        f"🕐 _{_ts()}_"
    )


async def send_telegram(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
    except TelegramError as e:
        print(f"[{_ts()}] Telegram error: {e}")


# ── Notion ───────────────────────────────────────────────────────────────────
async def update_notion_page(notion: NotionClient, items: list[dict]):
    """Append new market entries to the Notion page as a table-style block."""
    if not items:
        return

    children = []

    # Section header
    children.append({
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [{"type": "text", "text": {"content": f"📊 Actualización — {_ts()}"}}]
        },
    })

    for item in items:
        kind    = item.get("_kind", "market")
        title   = item.get("title") or item.get("question", "—")
        volume  = item.get("volume", 0)
        vol_fmt = f"${float(volume):,.0f}" if volume else "—"
        slug    = item.get("slug", "")
        url     = (
            f"https://polymarket.com/event/{slug}" if kind == "event"
            else f"https://polymarket.com/market/{slug}"
        )

        try:
            prices = json.loads(item.get("outcomePrices", "[]"))
            yes_p  = f"{float(prices[0])*100:.1f}%" if prices else "—"
        except Exception:
            yes_p = "—"

        line = f"{'🎯' if kind=='event' else '📈'} {title} | Vol: {vol_fmt} | YES: {yes_p}"

        children.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [
                    {"type": "text", "text": {"content": line, "link": {"url": url}}}
                ]
            },
        })

    children.append({
        "object": "block",
        "type": "divider",
        "divider": {},
    })

    try:
        await notion.blocks.children.append(block_id=NOTION_PAGE_ID, children=children)
        print(f"[{_ts()}] Notion actualizado con {len(items)} entradas.")
    except Exception as e:
        print(f"[{_ts()}] Notion error: {e}")


# ── Sell / close logic ───────────────────────────────────────────────────────
async def close_session(bot: Bot, notion: NotionClient, reason: str):
    """Called when the session ends or credits are about to run out."""
    msg = (
        f"⚠️ *Sesión cerrando*\n"
        f"Razón: {reason}\n"
        f"🔒 No hay posiciones abiertas — este bot es solo lectura.\n"
        f"🕐 _{_ts()}_"
    )
    await send_telegram(bot, msg)

    # Append closing note to Notion
    try:
        await notion.blocks.children.append(
            block_id=NOTION_PAGE_ID,
            children=[{
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {
                        "content": f"🔒 Sesión cerrada: {reason} — {_ts()}"
                    }}],
                    "icon": {"emoji": "⚠️"},
                    "color": "red_background",
                },
            }],
        )
    except Exception as e:
        print(f"[{_ts()}] Notion close error: {e}")

    print(f"[{_ts()}] Sesión cerrada: {reason}")


# ── Main loop ────────────────────────────────────────────────────────────────
async def poll_loop():
    bot    = Bot(token=TELEGRAM_TOKEN)
    notion = NotionClient(auth=NOTION_TOKEN)
    state  = load_state()

    # Startup message
    await send_telegram(bot, f"🚀 *Bot Polymarket iniciado*\n📡 Monitoreando mercados cada {POLL_INTERVAL//60} min.\n🕐 _{_ts()}_")
    print(f"[{_ts()}] Bot iniciado. Poll cada {POLL_INTERVAL}s.")

    try:
        async with aiohttp.ClientSession() as session:
            while not _shutdown.is_set():
                try:
                    print(f"[{_ts()}] Polling Polymarket...")
                    events  = await fetch_events(session)
                    markets = await fetch_markets(session)

                    new_events  = [e for e in events  if str(e.get("id")) not in state["seen_event_ids"]]
                    new_markets = [m for m in markets if str(m.get("id")) not in state["seen_market_ids"]]

                    notion_items = []

                    for ev in new_events:
                        await send_telegram(bot, format_event_message(ev))
                        state["seen_event_ids"].append(str(ev["id"]))
                        ev["_kind"] = "event"
                        notion_items.append(ev)
                        await asyncio.sleep(0.5)  # rate limit

                    for mk in new_markets:
                        await send_telegram(bot, format_market_message(mk))
                        state["seen_market_ids"].append(str(mk["id"]))
                        mk["_kind"] = "market"
                        notion_items.append(mk)
                        await asyncio.sleep(0.5)

                    if notion_items:
                        await update_notion_page(notion, notion_items)
                    else:
                        print(f"[{_ts()}] Sin novedades.")

                    # Keep state lists bounded
                    state["seen_event_ids"]  = state["seen_event_ids"][-500:]
                    state["seen_market_ids"] = state["seen_market_ids"][-500:]
                    state["last_poll"] = _ts()
                    save_state(state)

                except aiohttp.ClientError as e:
                    print(f"[{_ts()}] HTTP error: {e}")
                except Exception as e:
                    print(f"[{_ts()}] Error inesperado: {e}")

                # Wait for next poll or shutdown signal
                try:
                    await asyncio.wait_for(_shutdown.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass  # Normal — time to poll again

    finally:
        await close_session(bot, notion, "señal de cierre / fin de sesión")


if __name__ == "__main__":
    asyncio.run(poll_loop())
