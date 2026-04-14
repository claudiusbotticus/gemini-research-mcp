#!/usr/bin/env python3
"""
start_weatherbet.py — Lanzador robusto para Windows
=====================================================
Arranca el supervisor como proceso completamente independiente.
Ejecuta este script para iniciar todo el sistema WeatherBet.
"""
import subprocess
import sys
import os
from pathlib import Path

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = LOG_DIR / "supervisor.pid"


def is_running(pid: int) -> bool:
    """Comprueba si un PID está activo en Windows sin matarlo."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def start():
    # Comprobar si ya hay un supervisor corriendo
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if is_running(pid):
            print(f"Supervisor ya corriendo (PID {pid}). Nada que hacer.")
            return pid

    # Lanzar supervisor desacoplado del proceso actual
    proc = subprocess.Popen(
        [sys.executable, "-u", "supervisor.py"],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP |  # independiente de señales del padre
            subprocess.CREATE_NO_WINDOW           # sin ventana de consola
        ),
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"WeatherBet iniciado — Supervisor PID={proc.pid}")
    print(f"Logs en: {LOG_DIR}")
    print("Para parar: python start_weatherbet.py --stop")
    return proc.pid


def stop():
    if not PID_FILE.exists():
        print("No hay supervisor corriendo.")
        return
    pid = int(PID_FILE.read_text().strip())
    if is_running(pid):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"Supervisor (PID {pid}) detenido.")
    else:
        print(f"Supervisor (PID {pid}) ya no estaba corriendo.")
    PID_FILE.unlink(missing_ok=True)


def status():
    if not PID_FILE.exists():
        print("Sistema PARADO")
        return
    pid = int(PID_FILE.read_text().strip())
    if is_running(pid):
        print(f"Sistema ACTIVO — Supervisor PID={pid}")
    else:
        print(f"Sistema CAIDO — PID {pid} ya no existe (reinicia con: python start_weatherbet.py)")

    # Mostrar últimas líneas de cada log
    for name, log in [("Supervisor", "supervisor.log"), ("Bridge", "bridge.log"), ("WeatherBot", "weatherbot.log")]:
        log_path = LOG_DIR / log
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            last = next((l for l in reversed(lines) if l.strip()), "")
            print(f"  {name}: {last}")


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop()
    elif "--status" in sys.argv:
        status()
    else:
        start()
