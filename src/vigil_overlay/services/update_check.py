"""Asynchronous GitHub release check using Qt's network stack."""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from vigil_overlay.core.updates import (
    GITHUB_LATEST_RELEASE_API,
    ReleaseMetadataError,
    available_update_from_github,
)
from vigil_overlay.core.version import __version__

_LOGGER = logging.getLogger("vigil_overlay")
_REQUEST_TIMEOUT_MILLISECONDS = 8_000
_MAX_RESPONSE_BYTES = 512 * 1024


class UpdateCheckService(QObject):
    """Check once for a newer stable release without blocking the UI thread."""

    update_available = Signal(object)

    def __init__(
        self,
        *,
        current_version: str = __version__,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._network = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._completed = False

    def check(self) -> None:
        """Start the update request unless this instance already checked."""

        if self._completed or self._reply is not None:
            return

        request = QNetworkRequest(QUrl(GITHUB_LATEST_RELEASE_API))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"X-GitHub-Api-Version", b"2026-03-10")
        request.setRawHeader(
            b"User-Agent", f"VigilOverlay/{self._current_version}".encode("ascii")
        )
        request.setTransferTimeout(_REQUEST_TIMEOUT_MILLISECONDS)
        reply = self._network.get(request)
        self._reply = reply
        reply.finished.connect(lambda active_reply=reply: self._finish(active_reply))

    def stop(self) -> None:
        """Cancel an in-flight request during application shutdown."""

        reply = self._reply
        self._reply = None
        self._completed = True
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _finish(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        self._completed = True

        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                _LOGGER.info("Update check unavailable: %s", reply.errorString())
                return
            raw = bytes(reply.readAll().data())
            if len(raw) > _MAX_RESPONSE_BYTES:
                _LOGGER.warning("Ignored oversized GitHub update response")
                return
            payload = json.loads(raw.decode("utf-8"))
            update = available_update_from_github(
                payload,
                current_version=self._current_version,
            )
        except (json.JSONDecodeError, UnicodeError, ReleaseMetadataError) as exc:
            _LOGGER.warning("Ignored invalid GitHub update response: %s", exc)
            return
        finally:
            reply.deleteLater()

        if update is not None:
            _LOGGER.info(
                "Vigil Overlay update available: %s (current: %s)",
                update.latest_version,
                update.current_version,
            )
            self.update_available.emit(update)


__all__ = ["UpdateCheckService"]
