"""
Structured logging for DABOOK.

Every log record includes: timestamp, level, worker_id/supervisor, book_id,
task_id, kind (event category), and message.

Workers use process-local loggers; the supervisor aggregates via the DB
``events`` table for the dashboard log tail.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

DABOOK_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s"


def setup_logging(
    log_dir: Path,
    name: str,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """
    Configure a named logger that writes to *log_dir/<name>.log*.

    The supervisor uses ``name="supervisor"``; workers use their worker-id.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"dabook.{name}")
    logger.setLevel(level)
    if logger.handlers:
        return logger  # already configured

    fmt = logging.Formatter(DABOOK_LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S")

    file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"dabook.{name}")
