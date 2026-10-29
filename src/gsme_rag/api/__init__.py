"""Stateless parsing preview. Reviewed storage lives in storage.admin."""

__all__ = ["create_app"]


def create_app():
    """Import lazily so the parser remains usable without API dependencies."""
    from gsme_rag.api.app import create_app as _create_app

    return _create_app()
