"""Reference-manager providers (Zotero, Mendeley) behind types.LibraryProvider."""

from __future__ import annotations

from pathlib import Path

from ..types import LibraryError, LibraryProvider

__all__ = ["get_provider"]


def get_provider(cfg, home: Path | None = None) -> LibraryProvider | None:
    """The provider selected by cfg.library.provider, or None when it is "off"."""
    provider = (cfg.library.provider or "off").strip().lower()
    if provider == "off":
        return None
    if provider == "zotero":
        if cfg.zotero.source == "off":
            return None
        from .zotero import ZoteroProvider

        return ZoteroProvider(cfg)
    if provider == "mendeley":
        from .mendeley import MendeleyProvider

        token_path = Path(home) / cfg.mendeley_token_path.name if home is not None else None
        return MendeleyProvider(cfg, token_path=token_path)
    raise LibraryError(f"Unknown library.provider {cfg.library.provider!r} (use 'zotero', 'mendeley' or 'off').")
