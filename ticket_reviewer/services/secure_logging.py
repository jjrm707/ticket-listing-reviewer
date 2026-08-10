"""Process-wide fail-closed redaction for HTTP client diagnostic loggers."""

import logging
from threading import Lock


_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_INSTALL_LOCK = Lock()


class _HttpDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = "HTTP client request"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_FILTER = _HttpDiagnosticFilter()


def install_http_log_redaction() -> None:
    """Install one permanent filter before configured HTTP clients can be used."""
    with _INSTALL_LOCK:
        for name in _HTTP_LOGGERS:
            logger = logging.getLogger(name)
            if _FILTER not in logger.filters:
                logger.addFilter(_FILTER)
