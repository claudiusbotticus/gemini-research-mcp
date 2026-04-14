#!/usr/bin/env python3
"""
weatherbot_bridge.py — Puente WeatherBot → Polymarket REAL
============================================================
Lee señales del weatherbot y ejecuta órdenes reales via py-clob-client.
Abre posiciones (BUY) y las cierra (SELL) cuando el weatherbot lo indica:
  - stop_loss         : precio cayó 20% del entry
  - trailing_stop     : subió 20% y luego bajó al breakeven
  - forecast_changed  : previsión se alejó 2+ grados del bucket apostado
  - resolved          : mercado cerrado en Polymarket (no hay que vender)
"""

import asyncio
import json
import math
import os
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Forzar UTF-8 en stdout para Windows (evita UnicodeEncodeError en print)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

SAFE_ADDRESS = "0xd3842227909efc0893047c88822cde2db750130e"
os.environ["POLY_SAFE_ADDRESS"] = SAFE_ADDRESS

import requests as _req
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams
from py_clob_client.constants import POLYGON

# ── Config ────────────────────────────────────────────────────────────────────
PRIVATE_KEY    = os.environ["POLY_PRIVATE_KEY"]
MAX_BET        = float(os.getenv("BRIDGE_MAX_BET", "5.0"))
POLL_INTERVAL  = int(os.getenv("BRIDGE_POLL_SECONDS", "60"))
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_TOKEN2 = os.getenv("TELEGRAM_BOT_TOKEN_2", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SHARES     = 5.0    # mínimo en shares
MIN_COST_USD   = 1.0    # mínimo en USD (Polymarket exige >= $1 por orden marketable)

WEATHERBOT_DIR = Path(__file__).parent.parent / "weatherbot"
MARKETS_DIR    = WEATHERBOT_DIR / "data" / "markets"
BRIDGE_STATE   = Path(__file__).parent / "bridge_state.json"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _date_utc():
    return datetime.now(timezone.utc).strftime("%d/%m/%Y")


def _tg(text: str):
    if not TELEGRAM_CHAT:
        return
    for token in filter(None, [TELEGRAM_TOKEN, TELEGRAM_TOKEN2]):
        try:
            _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"},
                timeout=8,
            )
        except Exception:
            pass


def load_bridge_state() -> dict:
    if BRIDGE_STATE.exists():
        state = json.loads(BRIDGE_STATE.read_text())
    else:
        state = {}
    # Estructura garantizada
    state.setdefault("placed_orders", {})   # market_id → {order_id, clob_token, size, price}
    state.setdefault("closed_orders", {})   # market_id → sell_order_id | "resolved"
    state.setdefault("sell_errors", {})     # market_id → {count, last_error}
    return state


def save_bridge_state(state: dict):
    BRIDGE_STATE.write_text(json.dumps(state, indent=2))


def load_all_markets() -> list:
    if not MARKETS_DIR.exists():
        return []
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets


def get_clob_token_id(market_id: str) -> str | None:
    """Convierte market_id gamma → clobTokenId[0] (YES token)."""
    try:
        r = _req.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=8)
        raw = r.json().get("clobTokenIds", [])
        if isinstance(raw, str):
            raw = json.loads(raw)
        return str(raw[0]) if raw else None
    except Exception as e:
        print(f"  [TOKEN] {market_id}: {e}")
    return None


def get_real_shares(client: ClobClient, clob_token: str) -> float:
    """Consulta el balance real de un token en nuestra cuenta (en shares)."""
    try:
        params = BalanceAllowanceParams(
            asset_type="CONDITIONAL",
            token_id=clob_token,
            signature_type=1,
        )
        resp = client.get_balance_allowance(params)
        raw = int(resp.get("balance", "0"))
        return raw / 1_000_000   # Polymarket usa 6 decimales como USDC
    except Exception as e:
        print(f"  [BALANCE] Error: {e}")
        return 0.0


def build_clob_client() -> ClobClient:
    """Crea cliente oficial de Polymarket CLOB con creds L2 derivadas."""
    c0 = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        signature_type=1,
        funder=SAFE_ADDRESS,
    )
    l2 = c0.create_or_derive_api_creds()
    print(f"[{_ts()}] L2 creds derivadas: {l2.api_key}")
    return ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON,
        creds=l2,
        signature_type=1,
        funder=SAFE_ADDRESS,
    )


# ── Resumen diario ────────────────────────────────────────────────────────────
def _send_daily_summary(bridge: dict):
    """Genera y envía el resumen diario de operaciones a Telegram."""
    markets = load_all_markets()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    buys_today     = 0
    sells_today    = 0
    resolved_today = 0
    pnl_total      = 0.0
    open_count     = 0
    wins           = 0
    losses         = 0

    for mkt in markets:
        pos = mkt.get("position")
        if not pos:
            continue
        market_id = pos.get("market_id") or mkt.get("market_id")

        # Posiciones abiertas (colocadas pero no cerradas)
        if market_id in bridge["placed_orders"] and market_id not in bridge["closed_orders"]:
            open_count += 1

        # Operaciones cerradas hoy
        closed_at = pos.get("closed_at", "")
        if closed_at and today_str in closed_at:
            reason = pos.get("close_reason", "")
            pnl    = pos.get("pnl", 0.0) or 0.0
            if reason == "resolved":
                resolved_today += 1
            else:
                sells_today += 1
            pnl_total += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1

        # Compras de hoy (aproximado por opened_at si existe)
        opened_at = pos.get("opened_at", "")
        if opened_at and today_str in opened_at and market_id in bridge["placed_orders"]:
            buys_today += 1

    total_closed = wins + losses
    win_rate = f"{round(wins/total_closed*100)}%" if total_closed > 0 else "N/D"
    pnl_icon = "🟢" if pnl_total >= 0 else "🔴"

    lines = [
        f"*Resumen del dia — {datetime.now(timezone.utc).strftime('%d/%m/%Y')}*",
        "",
        f"*Compras ejecutadas:* {buys_today}",
        f"*Ventas ejecutadas:* {sells_today}",
        f"*Mercados resueltos por Polymarket:* {resolved_today}",
        f"*Aciertos / Fallos:* {wins} / {losses}",
        f"*Tasa de acierto:* {win_rate}",
        f"*Resultado neto del dia:* {pnl_icon} {'+'if pnl_total>=0 else ''}{pnl_total:.2f}$",
        "",
        f"*Posiciones abiertas actualmente:* {open_count}",
    ]

    # Ventas con problemas pendientes (agrupadas, sin spam individual)
    failed_sells = {mid: info for mid, info in bridge.get("sell_errors", {}).items() if info.get("count", 0) >= 3}
    if failed_sells:
        lines.append("")
        lines.append(f"*Ventas pendientes con error ({len(failed_sells)}):*")
        for mid, info in failed_sells.items():
            city   = info.get("city", "?")
            date   = info.get("date", "?")
            reason = info.get("reason", "?")
            lines.append(f"  - {city} {date} ({reason}): requiere revision manual")

    lines += ["", f"_{_ts()}_"]
    _tg("\n".join(lines))


# ── Migración de estado antiguo ───────────────────────────────────────────────
def migrate_state(state: dict) -> dict:
    """Convierte placed_orders antiguo (str) al nuevo formato (dict)."""
    for mid, val in list(state["placed_orders"].items()):
        if isinstance(val, str):
            if val.startswith("0x"):
                state["placed_orders"][mid] = {
                    "order_id": val,
                    "clob_token": None,   # se rellenará la próxima vez
                    "size": None,
                    "price": None,
                }
            else:
                # skipped_low_price u otro string especial → marcar como cerrado
                state["closed_orders"][mid] = val
                del state["placed_orders"][mid]
    return state


# ── Loop principal ────────────────────────────────────────────────────────────
async def run_bridge():
    client = build_clob_client()
    bridge = load_bridge_state()
    bridge = migrate_state(bridge)
    save_bridge_state(bridge)

    print(f"[{_ts()}] Bridge arrancado. Safe: {SAFE_ADDRESS} | Max bet: ${MAX_BET}")

    _running = True
    _last_summary_date  = None   # Evita mandar el resumen más de una vez al día
    _last_heartbeat_ts  = 0.0    # Último heartbeat enviado (epoch seconds)
    HEARTBEAT_INTERVAL  = 3 * 3600  # Cada 3 horas

    def _on_signal(sig, frame):
        nonlocal _running
        _running = False

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    import time as _time

    while _running:
        now_utc  = datetime.now(timezone.utc)
        now_epoch = _time.time()

        # ── Resumen diario a las 23:00 UTC ──────────────────────────────────
        if now_utc.hour == 23 and _last_summary_date != now_utc.date():
            _last_summary_date = now_utc.date()
            try:
                _send_daily_summary(bridge)
                print(f"[{_ts()}] Resumen diario enviado a Telegram.")
            except Exception as e:
                print(f"[{_ts()}] Error en resumen diario: {e}")

        # ── Heartbeat cada 3 horas ───────────────────────────────────────────
        if now_epoch - _last_heartbeat_ts >= HEARTBEAT_INTERVAL:
            _last_heartbeat_ts = now_epoch
            try:
                markets_hb = load_all_markets()
                placed  = bridge["placed_orders"]
                closed  = bridge["closed_orders"]
                open_pos = [(m.get("city_name", m.get("city","?")), m.get("date","?"),
                             m.get("position",{}).get("entry_price",0))
                            for m in markets_hb
                            if m.get("position")
                            and (m["position"].get("market_id") or m.get("market_id")) in placed
                            and (m["position"].get("market_id") or m.get("market_id")) not in closed]
                sells_ok = sum(1 for v in closed.values() if isinstance(v,str) and v.startswith("0x"))
                resolved = sum(1 for v in closed.values() if v == "resolved")

                pos_lines = "\n".join(
                    f"  • {city} {date} @ ${entry:.3f}"
                    for city, date, entry in sorted(open_pos, key=lambda x: x[1])
                ) or "  Ninguna por el momento"

                _tg(
                    f"✅ *Sistema funcionando correctamente*\n\n"
                    f"*Posiciones abiertas:* {len(open_pos)}\n"
                    f"{pos_lines}\n\n"
                    f"*Ventas ejecutadas (total):* {sells_ok}\n"
                    f"*Resueltos por Polymarket:* {resolved}\n\n"
                    f"_{_ts()}_"
                )
                print(f"[{_ts()}] Heartbeat enviado a Telegram.")
            except Exception as e:
                print(f"[{_ts()}] Error en heartbeat: {e}")

        markets = load_all_markets()

        # ── 1. CERRAR posiciones que el weatherbot marcó como cerradas ──────
        for mkt in markets:
            pos = mkt.get("position")
            if not pos:
                continue
            market_id   = pos.get("market_id") or mkt.get("market_id")
            pos_status  = pos.get("status", "open")
            mkt_status  = mkt.get("status", "open")

            # Solo actuar si: weatherbot cerró Y bridge la abrió Y bridge aún no la cerró
            if market_id not in bridge["placed_orders"]:
                continue
            if market_id in bridge["closed_orders"]:
                continue

            placed = bridge["placed_orders"][market_id]

            # Mercado resuelto en Polymarket → no hay que vender, se liquida sólo
            if mkt_status == "resolved":
                outcome = mkt.get("resolved_outcome", "?")
                pnl     = pos.get("pnl", 0.0)
                city    = mkt.get("city_name", mkt.get("city", "?"))
                date    = mkt.get("date", "?")
                bridge["closed_orders"][market_id] = "resolved"
                save_bridge_state(bridge)
                icon = "✅" if outcome == "win" else "❌"
                outcome_text = "Acertado" if outcome == "win" else "Fallado"
                print(f"[{_ts()}] RESUELTO {city} {date} → {outcome} | PnL: {'+' if pnl>=0 else ''}{pnl:.2f}")
                _tg(
                    f"{icon} *Mercado resuelto por Polymarket*\n\n"
                    f"*Mercado:* {city} — {date}\n"
                    f"*Resultado:* {outcome_text}\n"
                    f"*Beneficio/Perdida:* {'+'if pnl>=0 else ''}{pnl:.2f}$\n\n"
                    f"_{_ts()}_"
                )
                continue

            # Weatherbot cerró la posición (stop-loss, trailing, forecast) → SELL
            if pos_status == "closed" and pos.get("close_reason") != "resolved":
                exit_price   = pos.get("exit_price")
                close_reason = pos.get("close_reason", "manual")
                shares       = pos.get("shares") or placed.get("size")
                clob_token   = placed.get("clob_token")
                city         = mkt.get("city_name", mkt.get("city", "?"))
                date         = mkt.get("date", "?")

                if not clob_token:
                    clob_token = get_clob_token_id(market_id)
                    if clob_token:
                        placed["clob_token"] = clob_token
                        save_bridge_state(bridge)

                if not clob_token:
                    print(f"  [SELL-SKIP] Sin clobToken para {market_id}")
                    continue
                if exit_price is None:
                    print(f"  [SELL-SKIP] exit_price None para {market_id}")
                    continue
                if exit_price is not None and exit_price < 0.01:
                    # Precio 0 o casi 0 = mercado perdido/liquidado sin valor — no compensa vender
                    print(f"  [SKIP] exit_price={exit_price} para {market_id} (perdido/liquidado)")
                    bridge["closed_orders"][market_id] = "zero_exit_no_sell"
                    bridge["sell_errors"].pop(market_id, None)
                    save_bridge_state(bridge)
                    continue

                # Parar si este mercado ya falló demasiadas veces (evita spam de reintentos)
                sell_err_info = bridge["sell_errors"].get(market_id, {})
                if sell_err_info.get("count", 0) >= 3:
                    # Silencio total — se incluirá en el resumen diario
                    continue

                # Consultar balance REAL del token en nuestra cuenta
                real_shares = get_real_shares(client, clob_token)
                if real_shares < MIN_SHARES:
                    print(f"  [SELL-SKIP] Balance real {real_shares:.6f} < minimo. Mercado expirado/no-llenado.")
                    bridge["closed_orders"][market_id] = "expired_or_unfilled"
                    save_bridge_state(bridge)
                    continue

                sell_price = round(max(exit_price - 0.01, 0.01), 2)
                # Floor (no round) para nunca pedir más shares de las que tenemos
                sell_shares = math.floor(real_shares * 100) / 100

                print(f"[{_ts()}] CERRANDO {city} {date} | motivo={close_reason} | ${sell_price:.3f} x {sell_shares} (balance real)")
                try:
                    order_args = OrderArgs(
                        token_id=clob_token,
                        price=sell_price,
                        size=sell_shares,
                        side="SELL",
                    )
                    resp = client.create_and_post_order(order_args)
                    print(f"  Respuesta SELL: {resp}")

                    sell_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
                    ok      = bool(sell_id) or resp.get("status") in ("matched", "live", "delayed")

                    entry  = pos.get("entry_price", sell_price)
                    pnl    = round((sell_price - entry) * sell_shares, 2)

                    if ok:
                        bridge["closed_orders"][market_id] = sell_id or "sold"
                        bridge["sell_errors"].pop(market_id, None)  # limpiar errores previos
                        save_bridge_state(bridge)
                        icon = "🟢" if pnl >= 0 else "🔴"
                        reason_labels = {
                            "take_profit": "Take profit alcanzado",
                            "stop_loss": "Stop loss activado",
                            "trailing_stop": "Trailing stop activado",
                            "forecast_changed": "Prevision meteorologica cambiada",
                        }
                        reason_text = reason_labels.get(close_reason, close_reason)
                        _tg(
                            f"{icon} *Venta ejecutada*\n\n"
                            f"*Mercado:* {city} — {date}\n"
                            f"*Motivo del cierre:* {reason_text}\n"
                            f"*Precio de entrada:* ${entry:.3f}\n"
                            f"*Precio de salida:* ${sell_price:.3f}\n"
                            f"*Cantidad vendida:* {sell_shares} shares\n"
                            f"*Resultado:* {'+'if pnl>=0 else ''}{pnl:.2f}$\n\n"
                            f"_{_ts()}_"
                        )
                    else:
                        err = resp.get("error", resp.get("errorMsg", str(resp)))
                        print(f"  SELL FAILED: {err}")
                        # Acumular fallo silenciosamente
                        entry_err = bridge["sell_errors"].setdefault(market_id, {"count": 0, "city": city, "date": date, "reason": close_reason})
                        entry_err["count"] += 1
                        entry_err["last_error"] = err[:120]
                        save_bridge_state(bridge)

                except Exception as e:
                    err_str = str(e)
                    print(f"  SELL ERROR: {err_str}")
                    if "does not exist" in err_str or "orderbook" in err_str.lower():
                        print(f"  -> Mercado expirado, marcando como resuelto")
                        bridge["closed_orders"][market_id] = "market_expired"
                        bridge["sell_errors"].pop(market_id, None)
                        save_bridge_state(bridge)
                    else:
                        # Acumular fallo silenciosamente
                        entry_err = bridge["sell_errors"].setdefault(market_id, {"count": 0, "city": city, "date": date, "reason": close_reason})
                        entry_err["count"] += 1
                        entry_err["last_error"] = err_str[:120]
                        save_bridge_state(bridge)

                await asyncio.sleep(1)

        # ── 2. ABRIR nuevas posiciones ───────────────────────────────────────
        pending = [
            m for m in markets
            if m.get("position")
            and m["position"].get("status") == "open"
            and (m["position"].get("market_id") or m.get("market_id")) not in bridge["placed_orders"]
            and m.get("status") == "open"
        ]

        if not pending:
            print(f"[{_ts()}] Sin señales nuevas. Esperando {POLL_INTERVAL}s...")

        for mkt in pending:
            pos       = mkt["position"]
            market_id = pos.get("market_id") or mkt.get("market_id")
            price     = pos["entry_price"]
            ev        = pos.get("ev", 0)
            city      = mkt.get("city_name", mkt.get("city", "?"))
            date      = mkt.get("date", "?")
            unit      = mkt.get("unit", "")
            bucket    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit}"

            clob_token = get_clob_token_id(market_id)
            if not clob_token:
                print(f"  [SKIP] No clobTokenId para {market_id}")
                continue

            # Gestión de tamaño
            if price < 0.01:
                print(f"  [SKIP] Precio < 0.01 para {market_id}")
                bridge["placed_orders"][market_id] = {
                    "order_id": "skipped_low_price",
                    "clob_token": clob_token,
                    "size": 0,
                    "price": price,
                }
                bridge["closed_orders"][market_id] = "skipped_low_price"
                save_bridge_state(bridge)
                continue

            size_by_budget = round(MAX_BET / price, 2)
            size = max(size_by_budget, MIN_SHARES)
            cost = size * price
            if cost > MAX_BET:
                size = MIN_SHARES
                cost = size * price
            size = round(size, 2)
            cost = round(size * price, 4)

            # Polymarket exige >= $1 de coste total por orden
            if cost < MIN_COST_USD:
                needed = math.ceil(MIN_COST_USD / price)
                size = max(needed, MIN_SHARES)
                cost = round(size * price, 4)

            print(f"[{_ts()}] BUY {city} {date} {bucket} | ${price:.3f} x {size} | EV {ev:+.2f}")
            print(f"  token: {clob_token[:25]}...")

            try:
                order_args = OrderArgs(
                    token_id=clob_token,
                    price=round(price, 2),
                    size=size,
                    side="BUY",
                )
                resp = client.create_and_post_order(order_args)
                print(f"  Respuesta BUY: {resp}")

                order_id = resp.get("orderID") or resp.get("id") or resp.get("order_id")
                success  = bool(order_id) or resp.get("status") in ("matched", "live", "delayed")

                if success:
                    bridge["placed_orders"][market_id] = {
                        "order_id": order_id or "placed",
                        "clob_token": clob_token,
                        "size": size,
                        "price": price,
                    }
                    save_bridge_state(bridge)
                    print(f"  OK order_id={order_id}")
                    cost_usd = round(size * price, 2)
                    _tg(
                        f"*Compra ejecutada*\n\n"
                        f"*Mercado:* {city} — {date}\n"
                        f"*Rango apostado:* {bucket}\n"
                        f"*Precio de entrada:* ${price:.3f}\n"
                        f"*Cantidad:* {size} shares (${cost_usd:.2f} invertidos)\n"
                        f"*Valor esperado:* {ev:+.2f}\n\n"
                        f"_{_ts()}_"
                    )
                else:
                    err = resp.get("error", resp.get("errorMsg", str(resp)))
                    print(f"  FAILED: {err}")

            except Exception as e:
                print(f"  ERROR: {e}")

            await asyncio.sleep(1)

        # ── 3. Esperar siguiente ciclo ───────────────────────────────────────
        for _ in range(POLL_INTERVAL):
            if not _running:
                break
            await asyncio.sleep(1)

    print(f"\n[{_ts()}] Cerrando bridge...")


if __name__ == "__main__":
    asyncio.run(run_bridge())
