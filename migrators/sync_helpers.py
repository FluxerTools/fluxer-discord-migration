"""Shared helpers for the reconfigure (two-way sync) flow."""

from typing import Any

# Discord channel types that Fluxer supports directly
TEXT_TYPES = (0, 5)
VOICE_TYPE = 2
CATEGORY_TYPE = 4
FORUM_TYPES = (15,)
STAGE_TYPE = 13


def fluxer_type_for(discord_type: int) -> int | None:
    """Map a Discord channel type to the Fluxer type used on migration.

    Returns None for types that are skipped entirely (e.g. stage channels).
    Forum channels are migrated as text, matching _migrate_channels.
    """
    if discord_type in TEXT_TYPES or discord_type in FORUM_TYPES:
        return 0
    if discord_type == VOICE_TYPE:
        return 2
    if discord_type == CATEGORY_TYPE:
        return 4
    return None


def confirm(prompt: str, *, default_no: bool = True) -> bool:
    """Ask a yes/no question on the console.

    Defaults to "no" so an empty answer never destroys anything.
    """
    suffix = "(y/n)" if default_no else "(Y/n)"
    try:
        answer = input(f"{prompt} {suffix}: ").strip().lower()
    except EOFError:
        return False
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def confirm_deletions(kind: str, names: list[str], logger: Any) -> bool:
    """Show what would be deleted and ask once for the whole group.

    Deletion is irreversible, so the full list is always printed first and the
    default is "no".
    """
    if not names:
        return False

    print()
    print(f"  The following {len(names)} Fluxer {kind} are not in Discord:")
    for name in names:
        print(f"     • {name}")
    print()
    print("  ⚠ Deleting cannot be undone.")

    approved = confirm(f"  Delete these {len(names)} {kind}?")
    logger.log(
        f"Deletion of {len(names)} {kind}: {'APPROVED' if approved else 'declined'}",
        "WARN" if approved else "INFO",
    )
    return approved


def summarise(created: int, updated: int, deleted: int, unchanged: int) -> str:
    """One-line result summary for a sync phase."""
    return (
        f"created {created}, updated {updated}, "
        f"deleted {deleted}, unchanged {unchanged}"
    )
