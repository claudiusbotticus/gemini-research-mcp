#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supervisor.py — Auto-restart supervisor para WeatherBet
========================================================
Mantiene corriendo weatherbot/bot_v2.py y polymarket-trading-bot/weatherbot_bridge.py.
Si alguno falla o se para, lo reinicia automáticamente.

Uso:
    python supervisor.py          # Arranca todo
    python supervisor.py --stop   # Para todo limpiamente
"""

import subprocess
import sys
import os
import time
import signal
import json
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Rutas ─────────────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent
WEATHERBOT = BASE / "weatherbot"
BRIDGE_DIR = BASE / "polymarket-trading-bot"
LOG_DIR    = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

WEATHERBOT_LOG = LOG_DIR / "weatherbot.log"
BRIDGE_LOG     = LOG_DIR / "bridge.log"
SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
PID_FILE       = LOG_DIR / "supervisor.pid"

# ── Telegram (desde .env del bridge) ──────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(str(BRIDGE_DIR / ".env"), override=True)
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_TOKEN2 = os.getenv("TELEGRAM_BOT_TOKEN_2", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _tg(text: str):
    for token in filter(None, [TELEGRAM_TOKEN, TELEGRAM_TOKEN2]):
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"},
                timeout=8,
            )
        except Exception:
            pass

def _log(msg: str):
    line = f"[{_ts()}] {msg}"
    print(line, flush=True)
    with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def _trim(text: str, max_chars: int) -> str:
    return text[-max_chars:] if len(text) > max_chars else text

def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"

def _classify_error(error_tail: str) -> str:
    if "ConnectionError" in error_tail or "Connection refused" in error_tail:
        return "Perdida de conexion a internet o a Polymarket"
    if "SSLError" in error_tail or "ssl" in error_tail.lower():
        return "Error SSL / certificado de red"
    if "not enough balance" in error_tail:
        return "Saldo insuficiente en la cuenta"
    if "UnicodeEncodeError" in error_tail or "charmap" in error_tail:
        return "Error de codificacion de caracteres"
    if "PolyApiException" in error_tail:
        return "Error de la API de Polymarket"
    if "KeyboardInterrupt" in error_tail:
        return "Interrupcion manual"
    return "Error inesperado en el proceso"

# ── Proceso gestionado ─────────────────────────────────────────────────────────
class ManagedProcess:
    def __init__(self, name, cmd, cwd, log_path, restart_delay=10, max_restarts=20):
        self.name          = name
        self.cmd           = cmd
        self.cwd           = cwd
        self.log_path      = log_path
        self.restart_delay = restart_delay
        self.max_restarts  = max_restarts
        self.proc          = None
        self.restarts      = 0
        self.started_at    = None
        self._log_fh       = None

    def start(self):
        if self._log_fh:
            try: self._log_fh.close()
            except: pass
        self._log_fh = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            stdout=self._log_fh,
            stderr=self._log_fh,
            env={
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8":       "1",
            },
        )
        self.started_at = time.time()
        _log(f"[{self.name}] Iniciado PID={self.proc.pid}")

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        # poll() returns None if still running, exit code otherwise
        return self.proc.poll() is None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log_fh:
            try: self._log_fh.close()
            except: pass
        self.proc = None

    def _capture_last_error(self) -> str:
        """Lee las últimas 15 líneas del log del proceso para diagnosticar el fallo."""
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-15:]) if lines else "(log vacío)"
        except Exception:
            return "(no se pudo leer el log)"

    def _record_crash(self, exit_code: int, uptime: float, error_tail: str):
        """Guarda el fallo en crashes.json para aprender de él."""
        crash_file = LOG_DIR / "crashes.json"
        try:
            crashes = json.loads(crash_file.read_text(encoding="utf-8")) if crash_file.exists() else []
        except Exception:
            crashes = []
        crashes.append({
            "process":   self.name,
            "ts":        _ts(),
            "exit_code": exit_code,
            "uptime_s":  uptime,
            "restart_n": self.restarts,
            "last_lines": error_tail,
        })
        # Guardar solo los últimos 100 crashes
        crash_file.write_text(json.dumps(crashes[-100:], indent=2, ensure_ascii=False), encoding="utf-8")

    def _apply_crash_fixes(self, error_tail: str):
        """
        Aplica correcciones conocidas según el tipo de error detectado.
        Cada fix se detecta por pattern en el traceback/log.
        """
        fixes_applied = []

        # FIX 1: UnicodeEncodeError en Windows → forzar PYTHONIOENCODING
        if "UnicodeEncodeError" in error_tail or "charmap" in error_tail:
            os.environ["PYTHONIOENCODING"] = "utf-8"
            os.environ["PYTHONUTF8"] = "1"
            fixes_applied.append("PYTHONIOENCODING=utf-8")

        # FIX 2: order_type unexpected keyword → ya corregido en código, solo log
        if "order_type" in error_tail and "unexpected keyword" in error_tail:
            fixes_applied.append("order_type-already-fixed")

        # FIX 3: Connection error → espera extra antes de reiniciar
        if "ConnectionError" in error_tail or "Connection refused" in error_tail:
            self.restart_delay = 60
            fixes_applied.append("restart_delay=60s")
        else:
            self.restart_delay = 10  # Reset a normal si no es conexión

        # FIX 4: SSL error → espera media
        if "SSLError" in error_tail or "ssl" in error_tail.lower():
            self.restart_delay = 30
            fixes_applied.append("restart_delay=30s (SSL)")

        # FIX 5: PolyApiException not enough balance → skip y continuar
        if "not enough balance" in error_tail:
            fixes_applied.append("balance-issue-noted")

        if fixes_applied:
            _log(f"[{self.name}] Fixes aplicados: {', '.join(fixes_applied)}")

        return fixes_applied

    def maybe_restart(self) -> bool:
        """Comprueba si el proceso necesita reinicio. Retorna True si lo reinició."""
        if self.is_alive():
            return False

        exit_code = self.proc.returncode if self.proc else -1
        uptime    = round(time.time() - (self.started_at or 0), 1)

        # Capturar el error antes de reiniciar
        error_tail = self._capture_last_error()
        self._record_crash(exit_code, uptime, error_tail)
        fixes = self._apply_crash_fixes(error_tail)

        # Clasificar el tipo de error para el mensaje
        error_category = _classify_error(error_tail)
        uptime_str     = _fmt_uptime(uptime)

        component_labels = {"weatherbot": "Motor de analisis meteorologico", "bridge": "Motor de ejecucion de ordenes"}
        component_name = component_labels.get(self.name, self.name)

        if self.restarts >= self.max_restarts:
            _log(f"[{self.name}] DEMASIADOS REINICIOS ({self.restarts}) — pausado")
            _tg(
                f"🚨 *WeatherBet — Proceso caido definitivamente*\n\n"
                f"*Componente:* {component_name}\n"
                f"*Fallos consecutivos:* {self.restarts}\n"
                f"*Causa probable:* {error_category}\n\n"
                f"El supervisor ha dejado de intentar reiniciarlo. Se requiere revision manual.\n\n"
                f"*Ultimo error registrado:*\n```\n{_trim(error_tail, 300)}\n```\n\n"
                f"_{_ts()}_"
            )
            return False

        self.restarts += 1
        fix_str = f" | Fixes: {', '.join(fixes)}" if fixes else ""
        _log(f"[{self.name}] Caído (exit={exit_code}, uptime={uptime}s). Reinicio #{self.restarts} en {self.restart_delay}s{fix_str}")

        icon = "⚠️" if self.restarts <= 2 else "🔴"
        _tg(
            f"{icon} *WeatherBet — Reinicio automatico*\n\n"
            f"*Componente:* {component_name}\n"
            f"*Tiempo activo antes del fallo:* {uptime_str}\n"
            f"*Causa probable:* {error_category}\n"
            f"*Intento de reinicio:* {self.restarts} de {self.max_restarts}\n\n"
            f"El sistema se reiniciara en {self.restart_delay} segundos.\n\n"
            f"*Ultimo error registrado:*\n```\n{_trim(error_tail, 250)}\n```\n\n"
            f"_{_ts()}_"
        )
        time.sleep(self.restart_delay)
        self.start()
        return True


# ── Lógica principal ───────────────────────────────────────────────────────────
def _kill_orphans(script_names: list):
    """Mata procesos python huérfanos por nombre de script antes de arrancar."""
    try:
        r = subprocess.run(
            'wmic process where "name like \'%python%\'" get ProcessId,CommandLine',
            shell=True, capture_output=True, text=True, timeout=10
        )
        my_pid = os.getpid()
        for line in r.stdout.splitlines():
            for script in script_names:
                if script in line:
                    parts = line.strip().split()
                    pid = int(parts[-1]) if parts and parts[-1].isdigit() else None
                    if pid and pid != my_pid:
                        try:
                            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                           capture_output=True, timeout=5)
                            _log(f"[init] Huerfano eliminado: {script} PID={pid}")
                        except Exception:
                            pass
    except Exception as e:
        _log(f"[init] Error matando huerfanos: {e}")


_RUNNING = True

def _on_signal(sig, frame):
    global _RUNNING
    _RUNNING = False

def run_supervisor():
    global _RUNNING

    # Guardar PID para poder pararlo externamente
    PID_FILE.write_text(str(os.getpid()))

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    python = sys.executable

    processes = [
        ManagedProcess(
            name="weatherbot",
            cmd=[python, "-u", "bot_v2.py"],
            cwd=WEATHERBOT,
            log_path=WEATHERBOT_LOG,
            restart_delay=15,
        ),
        ManagedProcess(
            name="bridge",
            cmd=[python, "-u", "weatherbot_bridge.py"],
            cwd=BRIDGE_DIR,
            log_path=BRIDGE_LOG,
            restart_delay=10,
        ),
    ]

    _log("=== Supervisor arrancando ===")

    # Matar procesos huérfanos del ciclo anterior antes de arrancar nuevos
    _kill_orphans(["weatherbot_bridge.py", "bot_v2.py"])

    for p in processes:
        p.start()

    last_alive_check = time.time()

    while _RUNNING:
        try:
            time.sleep(5)

            # Comprobar salud cada 30s
            if time.time() - last_alive_check >= 30:
                last_alive_check = time.time()
                for p in processes:
                    try:
                        p.maybe_restart()
                    except Exception as e:
                        _log(f"[{p.name}] Error en maybe_restart: {e}")

        except Exception as e:
            _log(f"[supervisor] Error en bucle principal: {e} — continuando")
            time.sleep(5)

    # Parada limpia
    _log("=== Supervisor deteniendo procesos ===")
    for p in processes:
        p.stop()
    if PID_FILE.exists():
        PID_FILE.unlink()
    _log("=== Supervisor parado ===")


def stop_supervisor():
    if not PID_FILE.exists():
        print("No hay supervisor corriendo (no se encontró logs/supervisor.pid)")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Supervisor (PID {pid}) detenido.")
    except ProcessLookupError:
        print(f"Proceso {pid} no encontrado.")
    if PID_FILE.exists():
        PID_FILE.unlink()


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop_supervisor()
    else:
        run_supervisor()
