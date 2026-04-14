import json, sys
from pathlib import Path

markets_dir = Path("weatherbot/data/markets")
markets = [json.loads(f.read_text(encoding="utf-8")) for f in markets_dir.glob("*.json")]

open_pos   = [m for m in markets if m.get("position") and m["position"].get("status") == "open" and m.get("status") == "open"]
closed_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "closed"]
resolved   = [m for m in markets if m.get("status") == "resolved"]
no_pos     = [m for m in markets if not m.get("position")]

print(f"\n=== WEATHERBOT ===")
print(f"Total mercados tracked : {len(markets)}")
print(f"Sin posicion           : {len(no_pos)}")
print(f"Posiciones ABIERTAS    : {len(open_pos)}")
for m in open_pos:
    p = m["position"]
    print(f"  {m.get('city_name', m.get('city','?'))} {m.get('date')} entry=${p['entry_price']} market_id={p['market_id']}")
print(f"Posiciones cerradas bot: {len(closed_pos)}")
print(f"Resueltos Polymarket   : {len(resolved)}")

print(f"\n=== BRIDGE ===")
bs = json.load(open("polymarket-trading-bot/bridge_state.json"))
placed_real = {k: v for k, v in bs["placed_orders"].items() if isinstance(v, dict) and str(v.get("order_id","")).startswith("0x")}
closed_ok   = {k: v for k, v in bs["closed_orders"].items() if v not in ("skipped_low_price", "expired_or_unfilled")}
skipped     = {k: v for k, v in bs.get("placed_orders", {}).items() if not str(v.get("order_id","") if isinstance(v,dict) else v).startswith("0x")}
print(f"BUYs reales ejecutados : {len(placed_real)}")
print(f"Cerradas/vendidas      : {len(closed_ok)}")
print(f"Skipped                : {len(skipped)}")

# Find market 1871620 (the noisy one)
for m in markets:
    pos = m.get("position") or {}
    if str(pos.get("market_id","")) == "1871620":
        print(f"\nMarket 1871620: close_reason={pos.get('close_reason')} exit_price={pos.get('exit_price')} pos_status={pos.get('status')}")

# Adaptive learning state
adp_file = Path("weatherbot/data/adaptive_config.json")
if adp_file.exists():
    adp = json.loads(adp_file.read_text(encoding="utf-8"))
    print(f"\n=== APRENDIZAJE ===")
    print(f"MIN_EV adaptado : {adp.get('min_ev')}")
    print(f"Kelly por ciudad: {adp.get('kelly_scale', {})}")
    print(f"Mejor fuente    : {adp.get('best_source', {})}")
    stats = adp.get("city_stats", {})
    if stats:
        print("Resultados:")
        for city, s in stats.items():
            print(f"  {city}: {s['wins']}W/{s['losses']}L wr={s['win_rate']:.0%} pnl={s['pnl']:+.2f}$")
else:
    print("\n[LEARN] Sin datos suficientes aun (<30 mercados resueltos)")

# Bridge pulse (last log line)
log = Path("polymarket-trading-bot/bridge_live.log")
if log.exists():
    lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    last_ts = next((l for l in reversed(lines) if "UTC" in l), "?")
    print(f"\nBridge ultimo pulso: {last_ts}")
