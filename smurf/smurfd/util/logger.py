"""Logging estructurado para SMURF.

Logger JSON-friendly con niveles configurables y categoría por componente
(SIP, RTP, PBX, API, etc). Pensado para ser leído por humanos en consola
y consumido por journald cuando corre como systemd unit.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

_INITIALIZED = False


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool):
        super().__init__(
            fmt="%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self.use_color:
            color = self.COLORS.get(record.levelname, "")
            return f"{color}{msg}{self.RESET}"
        return msg


def setup_logging(level: str = "INFO", logfile: Optional[str] = None) -> None:
    """Configura el logging global. Idempotente."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    use_color = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_ColorFormatter(use_color))
    root.addHandler(stream)

    if logfile:
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(_ColorFormatter(False))
        root.addHandler(fh)

    logging.Formatter.converter = time.gmtime


def get_logger(name: str) -> logging.Logger:
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(name)
