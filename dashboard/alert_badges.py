def alert_source_badge(source: str | None, tier: int | None, pinned: bool | None) -> str:
    """Maps a price_alerts row's source/tier/pinned to a short operator-facing badge."""
    if pinned or source == "manual":
        return "📌 Manual"
    if source == "key_levels_watch":
        return "🏗 Structural"
    if source == "journal_sync":
        return "📓 Journal"
    return source or "—"
