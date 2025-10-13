from __future__ import annotations
import logging
import os
import sys

try:
    from colorlog import ColoredFormatter
except Exception:  # fallback if colorlog missing for any reason
    ColoredFormatter = None

_DEFAULT_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_DEFAULT_USE_COLOR = os.environ.get("LOG_USE_COLOR", "1").lower() in ("1", "true", "yes", "y")
_DEFAULT_FORMAT = os.environ.get(
    "LOG_FORMAT",
    "%(log_color)s[%(levelname)s]%(reset)s %(message_log_color)s%(message)s%(reset)s "
    "(%(name)s:%(lineno)d)"
)
_DEFAULT_DATEFMT = os.environ.get("LOG_DATEFMT", "%Y-%m-%d %H:%M:%S")

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

# Color choices for colorlog
_COLORS = {
    "DEBUG":    "cyan",
    "INFO":     "white",
    "WARNING":  "yellow",
    "ERROR":    "red",
    "CRITICAL": "bold_red",
}

def _is_tty(stream) -> bool:
    try:
        return stream.isatty()
    except Exception:
        return False

def get_logger(name: str = "sqs_fargate_listener") -> logging.Logger:
    """
    Create or fetch a configured logger. Honors LOG_LEVEL, LOG_USE_COLOR, LOG_FORMAT, LOG_DATEFMT.
    If color is disabled or not a TTY, uses plain formatting.
    """
    logger = logging.getLogger(name)
    if getattr(logger, "_sqs_fargate_listener_configured", False):
        return logger

    level = _LEVELS.get(_DEFAULT_LEVEL, logging.INFO)
    logger.setLevel(level)

    handler = logging.StreamHandler(stream=sys.stdout)
    use_color = _DEFAULT_USE_COLOR and ColoredFormatter is not None and _is_tty(sys.stdout)

    if use_color:
        formatter = ColoredFormatter(
            _DEFAULT_FORMAT,
            datefmt=_DEFAULT_DATEFMT,
            log_colors=_COLORS,
            secondary_log_colors={"message": _COLORS},
            reset=True,
            style="%",
        )
    else:
        # Plain format (CloudWatch renders best without ANSI)
        fmt = os.environ.get(
            "LOG_PLAIN_FORMAT",
            "[%(levelname)s] %(message)s (%(name)s:%(lineno)d)"
        )
        formatter = logging.Formatter(fmt=fmt, datefmt=_DEFAULT_DATEFMT)

    handler.setFormatter(formatter)
    logger.handlers[:] = [handler]  # replace any prior handlers
    logger.propagate = False
    logger._sqs_fargate_listener_configured = True  # type: ignore[attr-defined]
    return logger
