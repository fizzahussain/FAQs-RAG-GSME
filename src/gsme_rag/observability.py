"""Content-free stage logs for the local operator; never log prompts or secrets."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time


REQUEST_ID = ContextVar("gsme_request_id", default=None)
LOGGER = logging.getLogger("gsme_rag")
# An allowlist prevents accidental request bodies, tokens or file paths in logs.
FIELDS = {"request_id", "route", "method", "status", "elapsed_ms", "error_type", "stage",
          "job_id", "version_id", "backend", "model", "chunks", "warnings", "accepted", "held",
          "indexed", "failed", "candidates", "results", "missing_settings", "version"}


def emit(event: str, *, level=logging.INFO, **fields):
    record = {"event": event, "request_id": REQUEST_ID.get()}
    record.update({key: value for key, value in fields.items() if key in FIELDS})
    LOGGER.log(level, json.dumps(record, default=str, ensure_ascii=True))


@contextmanager
def operation(stage: str, **fields):
    start = time.perf_counter()
    emit(stage + ".started", **fields)
    try:
        yield
    except Exception as error:
        # Exception messages can contain provider request bodies or credentials.
        emit(stage + ".failed", level=logging.ERROR, error_type=type(error).__name__,
             elapsed_ms=round((time.perf_counter() - start) * 1000, 2), **fields)
        raise
    else:
        emit(stage + ".completed", elapsed_ms=round((time.perf_counter() - start) * 1000, 2), **fields)


def configure_console_logging(data_dir: Path):
    """Console plus bounded local files; repeated setup must not duplicate handlers."""
    level = os.getenv("GSME_LOG_LEVEL", "INFO").upper()
    LOGGER.setLevel(level if level in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO")
    LOGGER.propagate = False
    for handler in list(LOGGER.handlers):
        if getattr(handler, "_gsme_operator_handler", False):
            LOGGER.removeHandler(handler)
            handler.close()
    directory = data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            directory / "console.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")):
        handler._gsme_operator_handler = True
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
