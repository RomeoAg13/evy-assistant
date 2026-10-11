"""Pilotage de la télé en HDMI-CEC depuis la Pi (outil `cec-ctl`, paquet v4l-utils).

Sur Pi 5 : /dev/cec0 = port micro-HDMI 0 (le plus proche de l'alim), /dev/cec1 = port 1.
"""
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("evy.tv")

DEVICE = os.getenv("CEC_DEVICE", "/dev/cec0")
PHYS_ADDR = os.getenv("CEC_PHYS_ADDR", "")  # ex. "1.0.0.0" si la détection échoue
ENABLED = os.getenv("CEC_ENABLED", "true").lower() == "true"


def _run(*args: str) -> str:
    if not ENABLED:
        return ""
    if not shutil.which("cec-ctl"):
        log.warning("cec-ctl introuvable (sudo apt install v4l-utils)")
        return ""
    try:
        r = subprocess.run(
            ["cec-ctl", "-d", DEVICE, *args], capture_output=True, text=True, timeout=6
        )
        if r.returncode != 0:
            log.warning("cec-ctl %s : %s", args, r.stderr.strip())
        return r.stdout
    except Exception as e:  # noqa: BLE001
        log.warning("CEC indisponible : %s", e)
        return ""


def setup() -> None:
    """Déclare la Pi comme lecteur sur le bus CEC (à faire au démarrage)."""
    _run("--playback", "--osd-name", "Evy")


def _phys_addr() -> str:
    if PHYS_ADDR:
        return PHYS_ADDR
    m = re.search(r"Physical Address\s*:\s*([0-9a-fA-F.]+)", _run())
    return m.group(1) if m else ""


def wake() -> None:
    """Allume la télé et bascule sur l'entrée HDMI de la Pi."""
    _run("--to", "0", "--image-view-on")
    addr = _phys_addr()
    if addr and addr != "f.f.f.f":
        _run("--to", "0", "--active-source", f"phys-addr={addr}")


def standby() -> None:
    _run("--to", "0", "--standby")
