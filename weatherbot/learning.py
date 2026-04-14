"""
learning.py — Módulo de auto-aprendizaje para WeatherBet
=========================================================
Analiza los resultados históricos de mercados resueltos y ajusta:
  1. Kelly adaptativo por ciudad (basado en win-rate real)
  2. MIN_EV dinámico global (basado en rendimiento reciente)
  3. Ranking de fuentes de previsión por ciudad (menor sigma = más peso)

Los parámetros se guardan en data/adaptive_config.json y el bot los
carga en cada ciclo para tomar mejores decisiones de entrada/salida.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DATA_DIR       = Path("data")
ADAPTIVE_FILE  = DATA_DIR / "adaptive_config.json"
CALIBRATION_FILE = DATA_DIR / "calibration.json"

# Límites para evitar sobre-ajuste
KELLY_SCALE_MIN  = 0.60   # nunca bajar el Kelly más del 40%
KELLY_SCALE_MAX  = 1.40   # nunca subirlo más del 40%
MIN_EV_BASE      = 0.10   # valor base original
MIN_EV_MIN       = 0.05   # floor — nunca menos de 5%
MIN_EV_MAX       = 0.20   # ceiling — nunca más de 20%
MIN_SAMPLES      = 5      # mínimo de mercados resueltos para ajustar una ciudad
RECENT_WINDOW    = 20     # últimos N mercados para MIN_EV global


def load_adaptive() -> dict:
    if ADAPTIVE_FILE.exists():
        return json.loads(ADAPTIVE_FILE.read_text(encoding="utf-8"))
    return {
        "kelly_scale": {},     # city_slug → float (multiplicador)
        "min_ev":      MIN_EV_BASE,
        "best_source": {},     # city_slug → "ecmwf" | "hrrr" | "metar"
        "city_stats":  {},     # city_slug → {wins, losses, pnl, win_rate}
        "updated_at":  None,
    }


def save_adaptive(cfg: dict):
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    ADAPTIVE_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _city_stats(markets: list) -> dict:
    """Calcula win/loss/pnl por ciudad desde mercados resueltos."""
    stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "entries": []})
    for m in markets:
        if m.get("status") != "resolved":
            continue
        pos = m.get("position")
        if not pos or pos.get("status") not in ("closed",):
            continue
        city = m.get("city")
        if not city:
            continue
        outcome = m.get("resolved_outcome")
        pnl     = pos.get("pnl", 0.0) or 0.0
        if outcome == "win":
            stats[city]["wins"]  += 1
        elif outcome == "loss":
            stats[city]["losses"] += 1
        stats[city]["pnl"] += pnl
        stats[city]["entries"].append({
            "outcome": outcome,
            "pnl":     pnl,
            "price":   pos.get("entry_price"),
            "ev":      pos.get("ev"),
        })
    return dict(stats)


def _kelly_scale_for_city(wins: int, losses: int) -> float:
    """
    Calcula un multiplicador para el Kelly según win-rate de la ciudad.
    Win-rate > 60% → escala arriba (más confianza).
    Win-rate < 40% → escala abajo (menos confianza).
    Entre 40-60%   → sin cambio.
    """
    total = wins + losses
    if total < MIN_SAMPLES:
        return 1.0
    wr = wins / total
    if wr >= 0.70:
        scale = 1.40
    elif wr >= 0.60:
        scale = 1.20
    elif wr <= 0.30:
        scale = 0.60
    elif wr <= 0.40:
        scale = 0.80
    else:
        scale = 1.0
    return round(scale, 2)


def _dynamic_min_ev(markets: list) -> float:
    """
    Ajusta MIN_EV global según el rendimiento de los últimos RECENT_WINDOW mercados.
    Buen rendimiento  → umbral más bajo (entramos en más mercados)
    Mal rendimiento   → umbral más alto (somos más selectivos)
    """
    resolved = [
        m for m in markets
        if m.get("status") == "resolved" and m.get("resolved_outcome") in ("win", "loss")
    ]
    recent = resolved[-RECENT_WINDOW:]
    if len(recent) < 5:
        return MIN_EV_BASE

    wins   = sum(1 for m in recent if m.get("resolved_outcome") == "win")
    wr     = wins / len(recent)

    if wr >= 0.65:
        new_ev = MIN_EV_BASE * 0.85     # bajamos filtro → más oportunidades
    elif wr >= 0.55:
        new_ev = MIN_EV_BASE * 0.93
    elif wr <= 0.35:
        new_ev = MIN_EV_BASE * 1.20     # subimos filtro → más selectivos
    elif wr <= 0.45:
        new_ev = MIN_EV_BASE * 1.10
    else:
        new_ev = MIN_EV_BASE

    return round(min(max(new_ev, MIN_EV_MIN), MIN_EV_MAX), 4)


def _best_source_per_city(cal: dict) -> dict:
    """
    Para cada ciudad, devuelve la fuente con menor sigma (más precisa).
    Si no hay calibración suficiente para una ciudad, devuelve "ecmwf" (default).
    """
    best = {}
    cities = set(k.rsplit("_", 1)[0] for k in cal)
    for city in cities:
        candidates = {}
        for source in ["ecmwf", "hrrr", "metar"]:
            key = f"{city}_{source}"
            if key in cal and cal[key].get("n", 0) >= 3:
                candidates[source] = cal[key]["sigma"]
        if candidates:
            best[city] = min(candidates, key=candidates.get)
    return best


def run_learning(markets: list) -> dict:
    """
    Punto de entrada principal. Llámalo tras run_calibration().
    Retorna el adaptive_config actualizado.
    """
    cfg = load_adaptive()

    # 1. Estadísticas por ciudad
    stats = _city_stats(markets)
    city_stats_out = {}
    kelly_scale    = {}

    for city, s in stats.items():
        wins   = s["wins"]
        losses = s["losses"]
        total  = wins + losses
        wr     = wins / total if total > 0 else 0.0
        scale  = _kelly_scale_for_city(wins, losses)
        kelly_scale[city]    = scale
        city_stats_out[city] = {
            "wins":     wins,
            "losses":   losses,
            "pnl":      round(s["pnl"], 2),
            "win_rate": round(wr, 3),
            "kelly_scale": scale,
        }

    # 2. MIN_EV global dinámico
    new_min_ev = _dynamic_min_ev(markets)

    # 3. Mejor fuente por ciudad (desde calibration.json)
    cal = {}
    if CALIBRATION_FILE.exists():
        cal = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    best_source = _best_source_per_city(cal)

    # 4. Detectar cambios relevantes para logging
    changes = []
    old_ev = cfg.get("min_ev", MIN_EV_BASE)
    if abs(new_min_ev - old_ev) >= 0.005:
        changes.append(f"MIN_EV {old_ev:.3f}->{new_min_ev:.3f}")

    for city, scale in kelly_scale.items():
        old_scale = cfg.get("kelly_scale", {}).get(city, 1.0)
        if abs(scale - old_scale) >= 0.1:
            changes.append(f"Kelly[{city}] {old_scale:.2f}->{scale:.2f}")

    for city, src in best_source.items():
        old_src = cfg.get("best_source", {}).get(city, "ecmwf")
        if src != old_src:
            changes.append(f"Source[{city}] {old_src}->{src}")

    # 5. Guardar
    cfg["kelly_scale"] = kelly_scale
    cfg["min_ev"]      = new_min_ev
    cfg["best_source"] = best_source
    cfg["city_stats"]  = city_stats_out
    save_adaptive(cfg)

    if changes:
        print(f"  [LEARN] {' | '.join(changes)}")
    else:
        resolved_total = sum(s["wins"] + s["losses"] for s in stats.values())
        print(f"  [LEARN] Sin cambios — {resolved_total} mercados analizados")

    return cfg
