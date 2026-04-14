"""
Telegram notifications for weatherbet.py
Drop-in module: import and call notify_* functions.
Falls back silently if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set.
"""

import os
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOTION_TOKEN     = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID   = os.getenv("NOTION_PAGE_ID", "")

_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
_NOTION  = bool(NOTION_TOKEN and NOTION_PAGE_ID)


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _send(text: str):
    if not _ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        try:
            print(f"  [TG] send error: {e}")
        except Exception:
            pass


def _notion_append(blocks: list):
    if not _NOTION:
        return
    try:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            headers={
                "Authorization":  f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type":   "application/json",
            },
            json={"children": blocks},
            timeout=10,
        )
    except Exception as e:
        print(f"  [NOTION] error: {e}")


def _notion_bullet(text: str, url: str = None):
    rt = {"type": "text", "text": {"content": text}}
    if url:
        rt["text"]["link"] = {"url": url}
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [rt]},
    }


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def notify_start(n_cities: int, balance: float, scan_min: int):
    msg = (
        f"🚀 *WeatherBet arrancado*\n"
        f"🌍 {n_cities} ciudades | 💰 ${balance:,.0f} virtual\n"
        f"⏱ Scan cada {scan_min} min\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)


def notify_buy(city_name: str, date: str, bucket: str, entry: float,
               ev: float, cost: float, src: str, horizon: str):
    url = f"https://polymarket.com/event/highest-temperature-in-{city_name.lower().replace(' ', '-')}-on-{date}"
    msg = (
        f"📈 *BUY — {city_name}*\n"
        f"📅 {date} ({horizon}) | Bucket: `{bucket}`\n"
        f"💵 Entry: ${entry:.3f} | Cost: ${cost:.2f}\n"
        f"📊 EV: {ev:+.2f} | Src: {src.upper()}\n"
        f"🔗 [Ver en Polymarket]({url})\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"📈 BUY {city_name} {date} {bucket} | entry ${entry:.3f} | EV {ev:+.2f} | {src.upper()}",
            url,
        )
    ])


def notify_close(city_name: str, date: str, reason: str,
                 entry: float, exit_price: float, pnl: float):
    emoji = "✅" if pnl >= 0 else "❌"
    pnl_str = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
    msg = (
        f"{emoji} *{reason.upper()} — {city_name}*\n"
        f"📅 {date}\n"
        f"💵 Entry: ${entry:.3f} → Exit: ${exit_price:.3f}\n"
        f"💰 PnL: *{pnl_str}*\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"{emoji} {reason.upper()} {city_name} {date} | entry ${entry:.3f} exit ${exit_price:.3f} | PnL {pnl_str}"
        )
    ])


def notify_resolve(city_name: str, date: str, outcome: str, pnl: float,
                   balance: float):
    emoji = "🏆" if outcome == "win" else "💸"
    pnl_str = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
    msg = (
        f"{emoji} *{outcome.upper()} — {city_name}*\n"
        f"📅 {date} | PnL: *{pnl_str}*\n"
        f"💰 Balance: ${balance:,.2f}\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"{emoji} {outcome.upper()} {city_name} {date} | PnL {pnl_str} | Balance ${balance:,.2f}"
        )
    ])


def notify_new_markets(events: list):
    """Send summary of newly discovered Polymarket weather events."""
    if not events:
        return
    lines = [f"🆕 *{len(events)} nuevos mercados detectados*\n"]
    for ev in events[:5]:
        city = ev.get("city_name", "?")
        date = ev.get("date", "?")
        unit = ev.get("unit", "")
        fc   = ev.get("forecast")
        fc_s = f"{fc}°{unit}" if fc is not None else "—"
        lines.append(f"  • {city} {date} — forecast {fc_s}")
    if len(events) > 5:
        lines.append(f"  ...y {len(events)-5} más")
    lines.append(f"\n🕐 _{_ts()}_")
    _send("\n".join(lines))

    blocks = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"📊 {len(events)} nuevos mercados — {_ts()}"
                }}]
            },
        }
    ] + [
        _notion_bullet(
            f"{ev.get('city_name','?')} {ev.get('date','?')} | forecast {ev.get('forecast','—')}°{ev.get('unit','')}",
            f"https://polymarket.com/event/highest-temperature-in-{ev.get('city_name','').lower().replace(' ','-')}-on-{ev.get('date','')}",
        )
        for ev in events
    ] + [{"object": "block", "type": "divider", "divider": {}}]
    _notion_append(blocks)


def notify_status(balance: float, start: float, wins: int, losses: int,
                  open_pos: int):
    total   = wins + losses
    ret_pct = (balance - start) / start * 100 if start else 0
    wr      = f"{wins/total:.0%}" if total else "—"
    msg = (
        f"📊 *WeatherBet Status*\n"
        f"💰 Balance: ${balance:,.2f} ({'+' if ret_pct>=0 else ''}{ret_pct:.1f}%)\n"
        f"📈 Trades: {total} | W: {wins} | L: {losses} | WR: {wr}\n"
        f"🔓 Posiciones abiertas: {open_pos}\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)


def notify_shutdown(reason: str, balance: float, wins: int, losses: int):
    total = wins + losses
    wr    = f"{wins/total:.0%}" if total else "—"
    msg = (
        f"🔒 *WeatherBet cerrando*\n"
        f"Razón: {reason}\n"
        f"💰 Balance final: ${balance:,.2f}\n"
        f"📊 {total} trades | WR {wr}\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([{
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {
                "content": f"🔒 Sesión cerrada: {reason} — balance ${balance:,.2f} — {_ts()}"
            }}],
            "icon": {"emoji": "⚠️"},
            "color": "red_background",
        },
    }])
