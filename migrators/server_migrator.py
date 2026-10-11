"""Server migration from Discord to Fluxer."""

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from utils.ratelimit import RateLimitedClient

from .sync_helpers import (
    CATEGORY_TYPE,
    VOICE_TYPE,
    confirm,
    confirm_deletions,
    fluxer_type_for,
    summarise,
)


class ServerMigrator:
    """Handles migration of Discord servers to Fluxer."""

    def __init__(self, discord_http, fluxer_http, logger):
        self.discord_http = discord_http
        # Paces role/channel creation so Fluxer's 10-per-60s limit is respected
        # up front instead of being discovered through 429 retries.
        self.fluxer_http = RateLimitedClient(fluxer_http, logger)
        self.logger = logger
        # Maps Discord IDs to Fluxer IDs
        self.guild_id_map: dict[str, str] = {}
        self.role_id_map: dict[str, str] = {}
        self.category_id_map: dict[str, str] = {}
        self.channel_id_map: dict[str, str] = {}

    async def _download_image(self, url: str) -> bytes | None:
        """Download an image from URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as e:
            self.logger.log(f"Failed to download image: {e}", "ERROR")
        return None

    def _apng_to_gif(self, apng_data: bytes) -> bytes | None:
        """Convert APNG bytes to GIF bytes. Returns None on failure."""
        try:
            from apnggif import apnggif

            with (
                tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png_f,
                tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gif_f,
            ):
                png_path = png_f.name
                gif_path = gif_f.name
            try:
                Path(png_path).write_bytes(apng_data)
                apnggif(png_path, gif_path)
                return Path(gif_path).read_bytes()
            finally:
                Path(png_path).unlink(missing_ok=True)
                Path(gif_path).unlink(missing_ok=True)
        except Exception as e:
            self.logger.log(f"APNG to GIF conversion failed: {e}", "WARN")
            return None

    async def migrate_server(
        self,
        discord_guild: dict[str, Any],
        existing_fluxer_guild_id: str | None = None,
        migration_options: dict[str, bool] | None = None,
        partial_sync: bool = False,
    ) -> bool:
        """Migrate a single Discord server to Fluxer.

        Args:
            discord_guild: Discord guild data to migrate from
            existing_fluxer_guild_id: Optional ID of existing Fluxer guild to migrate into.
                                     If None, creates a new guild.
            migration_options: Dict specifying what to migrate. Keys:
                              - 'roles': Migrate roles (default True)
                              - 'channels': Migrate channels (default True)
                              - 'emojis': Migrate emojis (default True)
                              - 'stickers': Migrate stickers (default True)
                              - 'permissions': Migrate channel permissions (default True)
            partial_sync: If True, only adds missing items (matches by name). Default False.
        """
        if migration_options is None:
            migration_options = {
                "roles": True,
                "channels": True,
                "emojis": True,
                "stickers": True,
                "permissions": True,
            }
        guild_name = discord_guild["name"]
        guild_id = discord_guild["id"]

        self.logger.log(f"\n=== Migrating Server: {guild_name} ===")

        if not self.fluxer_http or not self.discord_http:
            self.logger.log("Clients not initialized", "ERROR")
            return False

        try:
            # Create or use existing guild on Fluxer
            if existing_fluxer_guild_id:
                fluxer_guild_id = existing_fluxer_guild_id
                self.logger.log(f"Using existing Fluxer guild: {fluxer_guild_id}")
                if partial_sync:
                    self.logger.log(
                        "⚡ SMART SYNC MODE: Will match by name and add only missing items"
                    )
                else:
                    self.logger.log(
                        "(Will add roles and channels, won't delete anything)"
                    )
            else:
                self.logger.log(f"Creating new guild: {guild_name}")

                guild_icon = None
                if discord_guild.get("icon"):
                    self.logger.log("Downloading guild icon...")
                    icon_hash = discord_guild["icon"]
                    icon_ext = "gif" if icon_hash.startswith("a_") else "png"
                    icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{icon_ext}?size=4096"
                    guild_icon = await self._download_image(icon_url)

                fluxer_guild = await self.fluxer_http.create_guild(
                    name=guild_name,
                    icon=guild_icon,
                )

                fluxer_guild_id = fluxer_guild["id"]
                self.logger.log(f"✓ Guild created with ID: {fluxer_guild_id}")

            self.guild_id_map[guild_id] = fluxer_guild_id

            # Fetch existing Fluxer items if doing partial sync
            existing_fluxer_items = {}
            if partial_sync and existing_fluxer_guild_id:
                self.logger.log(
                    "Fetching existing Fluxer server data for partial sync..."
                )
                try:
                    if migration_options.get("roles", True):
                        existing_fluxer_items[
                            "roles"
                        ] = await self.fluxer_http.get_guild_roles(fluxer_guild_id)
                    if migration_options.get("channels", True):
                        existing_fluxer_items[
                            "channels"
                        ] = await self.fluxer_http.get_guild_channels(fluxer_guild_id)
                    if migration_options.get("emojis", True):
                        existing_fluxer_items[
                            "emojis"
                        ] = await self.fluxer_http.get_guild_emojis(fluxer_guild_id)
                    if migration_options.get("stickers", True):
                        existing_fluxer_items[
                            "stickers"
                        ] = await self.fluxer_http.get_guild_stickers(fluxer_guild_id)
                except Exception as e:
                    self.logger.log(
                        f"Failed to fetch existing Fluxer data: {e}", "WARN"
                    )
                    existing_fluxer_items = {}

            # Fetch full guild data from Discord
            self.logger.log("Fetching server data from Discord...")

            # Fetch data based on what we need to migrate
            roles = []
            channels = []
            emojis = []
            stickers = []

            if migration_options.get("roles", True):
                roles = await self.discord_http.get_guild_roles(guild_id)

            if migration_options.get("channels", True) or migration_options.get(
                "permissions", True
            ):
                channels = await self.discord_http.get_guild_channels(guild_id)

            if migration_options.get("emojis", True):
                emojis = await self.discord_http.get_guild_emojis(guild_id)

            if migration_options.get("stickers", True):
                stickers = await self.discord_http.get_guild_stickers(guild_id)

            # Migrate in order: roles -> channels -> emojis
            if migration_options.get("roles", True):
                await self._migrate_roles(
                    roles, fluxer_guild_id, existing_fluxer_items.get("roles")
                )

            if migration_options.get("channels", True):
                await self._migrate_channels(
                    channels,
                    fluxer_guild_id,
                    migrate_permissions=migration_options.get("permissions", True),
                    existing_fluxer_channels=existing_fluxer_items.get("channels"),
                )

            if migration_options.get("emojis", True):
                await self._migrate_emojis(
                    emojis,
                    guild_id,
                    fluxer_guild_id,
                    existing_fluxer_items.get("emojis"),
                    save_to_disk=migration_options.get("save_to_disk", False),
                )

            if migration_options.get("stickers", True):
                await self._migrate_stickers(
                    stickers,
                    guild_id,
                    fluxer_guild_id,
                    existing_fluxer_items.get("stickers"),
                    save_to_disk=migration_options.get("save_to_disk", False),
                )

            self.logger.log(f"✓ Server '{guild_name}' migrated successfully!")
            return True

        except Exception as e:
            self.logger.log(f"Server migration failed: {e}", "ERROR")
            import traceback

            traceback.print_exc()
            return False

    async def reconfigure_server(
        self,
        discord_guild: dict[str, Any],
        fluxer_guild_id: str,
        phases: dict[str, bool] | None = None,
    ) -> bool:
        """Bring an already-migrated Fluxer server back in line with Discord.

        Unlike a migration this is a two-way sync: items missing on Fluxer are
        created, items that differ are updated, and items that exist only on
        Fluxer can be deleted. Every phase asks before it runs, and every
        deletion is listed and confirmed separately before anything is removed.
        """
        if phases is None:
            phases = {
                "settings": True,
                "roles": True,
                "channels": True,
                "emojis": True,
                "stickers": True,
            }

        guild_id = discord_guild["id"]
        self.logger.log(f"\n=== Reconfiguring: {discord_guild['name']} ===")
        self.logger.log(f"Target Fluxer guild: {fluxer_guild_id}")

        if phases.get("settings"):
            await self._sync_server_settings(discord_guild, fluxer_guild_id)

        # Roles must come before channels: permission overwrites depend on
        # role_id_map being populated.
        if phases.get("roles") or phases.get("channels"):
            await self._sync_roles(
                guild_id, fluxer_guild_id, apply_changes=bool(phases.get("roles"))
            )

        if phases.get("channels"):
            await self._sync_channels(guild_id, fluxer_guild_id)

        if phases.get("emojis"):
            await self._sync_emojis(guild_id, fluxer_guild_id)

        if phases.get("stickers"):
            await self._sync_stickers(guild_id, fluxer_guild_id)

        self.logger.log("\n=== Reconfigure complete ===")
        return True

    async def _sync_server_settings(
        self, discord_guild: dict[str, Any], fluxer_guild_id: str
    ):
        """Sync the guild name and icon."""
        self.logger.log("\n--- Server Settings ---")

        try:
            fluxer_guild = await self.fluxer_http.get_guild(fluxer_guild_id)
        except Exception as e:
            self.logger.log(f"Could not read Fluxer server settings: {e}", "WARN")
            fluxer_guild = {}

        discord_name = discord_guild.get("name")
        fluxer_name = fluxer_guild.get("name")

        changes: list[str] = []
        if discord_name and discord_name != fluxer_name:
            changes.append(f"name: '{fluxer_name}' -> '{discord_name}'")

        icon_hash = discord_guild.get("icon")
        update_icon = bool(icon_hash)
        if update_icon:
            changes.append("icon: re-upload from Discord")

        if not changes:
            self.logger.log("Server settings already match")
            return

        for change in changes:
            self.logger.log(f"  {change}")

        if not confirm("  Apply these server setting changes?"):
            self.logger.log("Skipped server settings", "INFO")
            return

        kwargs: dict[str, Any] = {}
        if discord_name and discord_name != fluxer_name:
            kwargs["name"] = discord_name

        if update_icon:
            ext = "gif" if str(icon_hash).startswith("a_") else "png"
            icon_url = (
                f"https://cdn.discordapp.com/icons/{discord_guild['id']}/"
                f"{icon_hash}.{ext}?size=512"
            )
            icon_data = await self._download_image(icon_url)
            if icon_data:
                kwargs["icon"] = icon_data
            else:
                self.logger.log("  Could not download guild icon", "WARN")

        if not kwargs:
            return

        try:
            await self.fluxer_http.modify_guild(fluxer_guild_id, **kwargs)
            self.logger.log("  ✓ Server settings updated")
        except Exception as e:
            self.logger.log(f"  Failed to update server settings: {e}", "WARN")

    async def _sync_roles(
        self, guild_id: str, fluxer_guild_id: str, apply_changes: bool = True
    ):
        """Create, update, reorder and optionally delete roles.

        When apply_changes is False the Fluxer roles are only mapped onto their
        Discord counterparts (so channel permissions still work) without
        changing anything.
        """
        if apply_changes:
            self.logger.log("\n--- Roles ---")

        try:
            discord_roles = await self.discord_http.get_guild_roles(guild_id)
            fluxer_roles = await self.fluxer_http.get_guild_roles(fluxer_guild_id)
        except Exception as e:
            self.logger.log(f"Failed to fetch roles: {e}", "ERROR")
            return

        fluxer_by_name = {r["name"]: r for r in fluxer_roles}

        everyone = next(
            (r for r in discord_roles if r.get("name") == "@everyone"), None
        )
        if everyone and "@everyone" in fluxer_by_name:
            self.role_id_map[everyone["id"]] = fluxer_by_name["@everyone"]["id"]

        wanted = [
            r
            for r in discord_roles
            if r.get("name") != "@everyone" and not r.get("managed")
        ]
        wanted_names = {r["name"] for r in wanted}

        # Map everything that already matches, so permissions work even if the
        # user declines the role changes.
        for role in wanted:
            match = fluxer_by_name.get(role["name"])
            if match:
                self.role_id_map[role["id"]] = match["id"]

        if not apply_changes:
            return

        to_create = [r for r in wanted if r["name"] not in fluxer_by_name]
        extra = [
            r
            for r in fluxer_roles
            if r.get("name") not in wanted_names and r.get("name") != "@everyone"
        ]

        created = updated = deleted = unchanged = 0

        # --- create missing ---
        if to_create:
            self.logger.log(f"\n{len(to_create)} role(s) missing on Fluxer:")
            for r in to_create:
                self.logger.log(f"  • {r['name']}")
            if confirm(f"  Create these {len(to_create)} role(s)?", default_no=False):
                for role in sorted(
                    to_create, key=lambda r: (r.get("position", 0), int(r["id"]))
                ):
                    name = role["name"]
                    color = role.get("color", 0)
                    permissions = int(role.get("permissions", 0))
                    hoist = role.get("hoist", False)
                    mentionable = role.get("mentionable", False)
                    self.logger.log(f"Creating role: {name}")
                    try:
                        new_role = await self.fluxer_http.create_guild_role(
                            fluxer_guild_id,
                            name=name,
                            permissions=permissions,
                            color=color,
                            hoist=hoist,
                            mentionable=mentionable,
                        )
                        self.role_id_map[role["id"]] = new_role["id"]
                        fluxer_by_name[name] = new_role
                        await self._apply_role_flags(
                            fluxer_guild_id,
                            new_role["id"],
                            name,
                            hoist=hoist,
                            mentionable=mentionable,
                        )
                        self.logger.log(f"  ✓ Created {name}")
                        created += 1
                    except Exception as e:
                        self.logger.log(f"  Failed to create '{name}': {e}", "WARN")
            else:
                self.logger.log("Skipped creating roles")

        # --- update changed ---
        differing = []
        for role in wanted:
            target = fluxer_by_name.get(role["name"])
            if not target:
                continue
            if (
                target.get("color") == role.get("color", 0)
                and bool(target.get("hoist")) == role.get("hoist", False)
                and bool(target.get("mentionable")) == role.get("mentionable", False)
            ):
                unchanged += 1
            else:
                differing.append((role, target))

        if differing:
            self.logger.log(f"\n{len(differing)} role(s) differ from Discord:")
            for role, _ in differing:
                self.logger.log(f"  • {role['name']}")
            if confirm(
                f"  Update these {len(differing)} role(s) "
                "(colour, permissions, hoist, mentionable)?",
                default_no=False,
            ):
                for role, target in differing:
                    name = role["name"]
                    try:
                        await self.fluxer_http.modify_guild_role(
                            fluxer_guild_id,
                            target["id"],
                            color=role.get("color", 0),
                            permissions=int(role.get("permissions", 0)),
                            hoist=role.get("hoist", False),
                            mentionable=role.get("mentionable", False),
                        )
                        self.logger.log(f"  ✓ Updated {name}")
                        updated += 1
                    except Exception as e:
                        self.logger.log(f"  Failed to update '{name}': {e}", "WARN")
            else:
                self.logger.log("Skipped role updates")

        # --- delete extras ---
        if extra:
            names = [r.get("name", "?") for r in extra]
            if confirm_deletions("ROLES", names, self.logger):
                for role in extra:
                    try:
                        await self.fluxer_http.delete_guild_role(
                            fluxer_guild_id, role["id"]
                        )
                        self.logger.log(f"  ✓ Deleted role {role.get('name')}")
                        deleted += 1
                    except Exception as e:
                        self.logger.log(
                            f"  Failed to delete '{role.get('name')}': {e}", "WARN"
                        )
                extra = []

        # --- reorder to match Discord ---
        ordered: list[str] = []
        for role in sorted(wanted, key=lambda r: (r.get("position", 0), int(r["id"]))):
            target = fluxer_by_name.get(role["name"])
            if target:
                ordered.append(target["id"])

        kept_ids = {r["id"] for r in extra}
        leftovers = [
            r
            for r in fluxer_roles
            if r["id"] in kept_ids and r.get("name") != "@everyone"
        ]
        ordered += [
            r["id"] for r in sorted(leftovers, key=lambda r: r.get("position", 0))
        ]

        if ordered:
            await self._reorder_roles(fluxer_guild_id, ordered)

        self.logger.log(
            "Roles: " + summarise(created, updated, deleted, unchanged)
        )

    async def _sync_channels(self, guild_id: str, fluxer_guild_id: str):
        """Create, update, reorder and optionally delete categories/channels."""
        self.logger.log("\n--- Channels & Categories ---")

        try:
            discord_channels = await self.discord_http.get_guild_channels(guild_id)
            fluxer_channels = await self.fluxer_http.get_guild_channels(
                fluxer_guild_id
            )
        except Exception as e:
            self.logger.log(f"Failed to fetch channels: {e}", "ERROR")
            return

        # Fluxer channels keyed by (type, name): a category and a text channel
        # may legitimately share a name.
        fluxer_by_key: dict[tuple[int, str], dict[str, Any]] = {}
        for ch in fluxer_channels:
            fluxer_by_key[(ch.get("type", 0), ch.get("name", ""))] = ch

        wanted: list[tuple[dict[str, Any], int]] = []
        for ch in discord_channels:
            mapped = fluxer_type_for(ch.get("type", 0))
            if mapped is None:
                self.logger.log(
                    f"Skipping unsupported channel: {ch.get('name')}", "WARN"
                )
                continue
            wanted.append((ch, mapped))

        wanted_keys = {(mapped, ch.get("name", "")) for ch, mapped in wanted}

        created = updated = deleted = unchanged = 0

        # --- categories first, so children can be parented ---
        for ch, mapped in sorted(
            wanted, key=lambda cm: cm[0].get("position", 0)
        ):
            if mapped != CATEGORY_TYPE:
                continue
            key = (mapped, ch["name"])
            existing = fluxer_by_key.get(key)
            if existing:
                self.category_id_map[ch["id"]] = existing["id"]

        to_create = [
            (ch, mapped)
            for ch, mapped in wanted
            if (mapped, ch.get("name", "")) not in fluxer_by_key
        ]
        extra = [
            ch
            for ch in fluxer_channels
            if (ch.get("type", 0), ch.get("name", "")) not in wanted_keys
        ]

        # --- create missing (categories before their children) ---
        if to_create:
            self.logger.log(f"\n{len(to_create)} channel(s) missing on Fluxer:")
            for ch, mapped in to_create:
                kind = "category" if mapped == CATEGORY_TYPE else (
                    "voice" if mapped == VOICE_TYPE else "text"
                )
                self.logger.log(f"  • [{kind}] {ch.get('name')}")
            if confirm(
                f"  Create these {len(to_create)} channel(s)?", default_no=False
            ):
                ordered_create = sorted(
                    to_create,
                    key=lambda cm: (
                        0 if cm[1] == CATEGORY_TYPE else 1,
                        cm[0].get("position", 0),
                    ),
                )
                for ch, mapped in ordered_create:
                    name = ch.get("name", "unnamed")
                    parent = None
                    if ch.get("parent_id"):
                        parent = self.category_id_map.get(ch["parent_id"])
                    self.logger.log(f"Creating channel: {name}")
                    try:
                        kwargs: dict[str, Any] = {
                            "name": name,
                            "type": mapped,
                            "position": ch.get("position", 0),
                        }
                        if mapped != CATEGORY_TYPE:
                            kwargs["parent_id"] = parent
                            kwargs["nsfw"] = ch.get("nsfw", False)
                            if ch.get("topic"):
                                kwargs["topic"] = ch["topic"]
                        new_ch = await self.fluxer_http.create_guild_channel(
                            fluxer_guild_id, **kwargs
                        )
                        fluxer_by_key[(mapped, name)] = new_ch
                        if mapped == CATEGORY_TYPE:
                            self.category_id_map[ch["id"]] = new_ch["id"]
                        else:
                            self.channel_id_map[ch["id"]] = new_ch["id"]
                        await self._apply_channel_permissions(ch, new_ch["id"])
                        self.logger.log(f"  ✓ Created {name}")
                        created += 1
                    except Exception as e:
                        self.logger.log(f"  Failed to create '{name}': {e}", "WARN")
            else:
                self.logger.log("Skipped creating channels")

        # --- update existing ---
        differing: list[tuple[dict[str, Any], int, dict[str, Any]]] = []
        for ch, mapped in wanted:
            target = fluxer_by_key.get((mapped, ch.get("name", "")))
            if not target:
                continue
            if mapped == CATEGORY_TYPE:
                self.category_id_map[ch["id"]] = target["id"]
            else:
                self.channel_id_map[ch["id"]] = target["id"]

            wants_parent = None
            if ch.get("parent_id"):
                wants_parent = self.category_id_map.get(ch["parent_id"])

            same = (
                (ch.get("topic") or "") == (target.get("topic") or "")
                and bool(ch.get("nsfw", False)) == bool(target.get("nsfw", False))
                and (mapped == CATEGORY_TYPE or wants_parent == target.get("parent_id"))
            )
            if same:
                unchanged += 1
            else:
                differing.append((ch, mapped, target))

        if differing:
            self.logger.log(f"\n{len(differing)} channel(s) differ from Discord:")
            for ch, _, _ in differing:
                self.logger.log(f"  • {ch.get('name')}")
            if confirm(
                f"  Update these {len(differing)} channel(s) "
                "(topic, nsfw, category) and re-apply permissions?",
                default_no=False,
            ):
                for ch, mapped, target in differing:
                    name = ch.get("name", "unnamed")
                    try:
                        kwargs: dict[str, Any] = {}
                        if mapped != CATEGORY_TYPE:
                            kwargs["nsfw"] = ch.get("nsfw", False)
                            if ch.get("topic"):
                                kwargs["topic"] = ch["topic"]
                            if ch.get("parent_id"):
                                parent = self.category_id_map.get(ch["parent_id"])
                                if parent:
                                    kwargs["parent_id"] = parent
                        if kwargs:
                            await self.fluxer_http.modify_channel(
                                target["id"], **kwargs
                            )
                        await self._apply_channel_permissions(ch, target["id"])
                        self.logger.log(f"  ✓ Updated {name}")
                        updated += 1
                    except Exception as e:
                        self.logger.log(f"  Failed to update '{name}': {e}", "WARN")
            else:
                self.logger.log("Skipped channel updates")

        # --- delete extras ---
        if extra:
            names = [
                f"[{'category' if c.get('type') == CATEGORY_TYPE else 'channel'}] "
                f"{c.get('name', '?')}"
                for c in extra
            ]
            if confirm_deletions("CHANNELS/CATEGORIES", names, self.logger):
                # Delete children before their categories
                for ch in sorted(
                    extra, key=lambda c: 1 if c.get("type") == CATEGORY_TYPE else 0
                ):
                    try:
                        await self.fluxer_http.delete_channel(ch["id"])
                        self.logger.log(f"  ✓ Deleted {ch.get('name')}")
                        deleted += 1
                    except Exception as e:
                        self.logger.log(
                            f"  Failed to delete '{ch.get('name')}': {e}", "WARN"
                        )

        self.logger.log(
            "Channels: " + summarise(created, updated, deleted, unchanged)
        )

    async def _sync_emojis(self, guild_id: str, fluxer_guild_id: str):
        """Create missing emojis and optionally delete extras."""
        self.logger.log("\n--- Emojis ---")

        try:
            discord_emojis = await self.discord_http.get_guild_emojis(guild_id)
            fluxer_emojis = await self.fluxer_http.get_guild_emojis(fluxer_guild_id)
        except Exception as e:
            self.logger.log(f"Failed to fetch emojis: {e}", "ERROR")
            return

        await self._sync_assets(
            kind="EMOJIS",
            label="emoji",
            discord_items=discord_emojis,
            fluxer_items=fluxer_emojis,
            create=lambda name, data: self.fluxer_http.create_guild_emoji(
                fluxer_guild_id, name=name, image=data
            ),
            delete=lambda item: self.fluxer_http.delete_guild_emoji(
                fluxer_guild_id, item["id"]
            ),
            url_for=lambda item: (
                "https://cdn.discordapp.com/emojis/"
                f"{item['id']}.{'webp' if item.get('animated') else 'png'}?size=128"
                + ("&animated=true" if item.get("animated") else "")
            ),
        )

    async def _sync_stickers(self, guild_id: str, fluxer_guild_id: str):
        """Create missing stickers and optionally delete extras."""
        self.logger.log("\n--- Stickers ---")

        try:
            discord_stickers = await self.discord_http.get_guild_stickers(guild_id)
            fluxer_stickers = await self.fluxer_http.get_guild_stickers(
                fluxer_guild_id
            )
        except Exception as e:
            self.logger.log(f"Failed to fetch stickers: {e}", "ERROR")
            return

        await self._sync_assets(
            kind="STICKERS",
            label="sticker",
            discord_items=discord_stickers,
            fluxer_items=fluxer_stickers,
            create=lambda name, data: self.fluxer_http.create_guild_sticker(
                fluxer_guild_id, name=name, image=data
            ),
            delete=lambda item: self.fluxer_http.delete_guild_sticker(
                fluxer_guild_id, item["id"]
            ),
            url_for=lambda item: (
                f"https://media.discordapp.net/stickers/{item['id']}.png?size=320"
            ),
        )

    async def _sync_assets(
        self,
        *,
        kind: str,
        label: str,
        discord_items: list[dict[str, Any]],
        fluxer_items: list[dict[str, Any]],
        create,
        delete,
        url_for,
    ):
        """Shared create/delete flow for emojis and stickers.

        Image contents cannot be compared without downloading both sides, so
        items present on both are left untouched; only additions and removals
        are handled.
        """
        fluxer_names = {i.get("name") for i in fluxer_items}
        discord_names = {i.get("name") for i in discord_items}

        to_create = [i for i in discord_items if i.get("name") not in fluxer_names]
        extra = [i for i in fluxer_items if i.get("name") not in discord_names]
        unchanged = len(discord_items) - len(to_create)

        created = deleted = 0

        if to_create:
            self.logger.log(f"\n{len(to_create)} {label}(s) missing on Fluxer:")
            for item in to_create:
                self.logger.log(f"  • :{item.get('name')}:")
            if confirm(
                f"  Create these {len(to_create)} {label}(s)?", default_no=False
            ):
                for item in to_create:
                    name = item.get("name", "unnamed")
                    data = await self._download_image(url_for(item))
                    if not data:
                        self.logger.log(f"  Could not download {label} '{name}'", "WARN")
                        continue
                    try:
                        await create(name, data)
                        self.logger.log(f"  ✓ Created :{name}:")
                        created += 1
                    except Exception as e:
                        self.logger.log(f"  Failed to create '{name}': {e}", "WARN")
                    await asyncio.sleep(0.3)
            else:
                self.logger.log(f"Skipped creating {label}s")

        if extra:
            names = [str(i.get("name", "?")) for i in extra]
            if confirm_deletions(kind, names, self.logger):
                for item in extra:
                    try:
                        await delete(item)
                        self.logger.log(f"  ✓ Deleted :{item.get('name')}:")
                        deleted += 1
                    except Exception as e:
                        self.logger.log(
                            f"  Failed to delete '{item.get('name')}': {e}", "WARN"
                        )
                    await asyncio.sleep(0.2)

        self.logger.log(
            f"{kind.title()}: " + summarise(created, 0, deleted, unchanged)
        )

    async def _apply_role_flags(
        self,
        fluxer_guild_id: str,
        fluxer_role_id: str,
        role_name: str,
        *,
        hoist: bool,
        mentionable: bool,
    ) -> bool:
        """Apply the role settings that Fluxer's create endpoint ignores.

        POST /guilds/{id}/roles accepts `hoist` and `mentionable` but always
        stores them as false, so "Show this role separately" and "Allow
        mentions" have to be set with a follow-up PATCH. Role edits are not
        rate limited, unlike creates.
        """
        if not hoist and not mentionable:
            # Nothing to change; false is already the created state.
            return True

        try:
            await self.fluxer_http.modify_guild_role(
                fluxer_guild_id,
                fluxer_role_id,
                hoist=hoist,
                mentionable=mentionable,
            )
            flags = []
            if hoist:
                flags.append("hoisted")
            if mentionable:
                flags.append("mentionable")
            self.logger.log(f"    ✓ Set {' + '.join(flags)}")
            return True
        except Exception as e:
            self.logger.log(
                f"    Failed to set hoist/mentionable for '{role_name}': {e}", "WARN"
            )
            return False

    async def _reorder_roles(
        self, fluxer_guild_id: str, ordered_role_ids: list[str]
    ):
        """Reorder roles to match the Discord hierarchy.

        Discord positions are sparse and guild-specific (gaps between values,
        managed roles mixed in), so they cannot be sent to Fluxer as-is. Instead
        we send a dense sequence: the lowest-ranked role gets position 1 and the
        highest-ranked role gets position len(ordered_role_ids). Position 0 is
        reserved for @everyone, which must stay at the bottom and is never
        included here.

        Args:
            fluxer_guild_id: Fluxer guild ID
            ordered_role_ids: Fluxer role IDs ordered lowest rank first
        """
        if not ordered_role_ids:
            return

        self.logger.log("\nReordering roles to match Discord hierarchy...")

        # Dense positions starting at 1 (@everyone keeps 0 at the bottom)
        payload = [
            {"id": role_id, "position": index}
            for index, role_id in enumerate(ordered_role_ids, start=1)
        ]

        # Fluxer/Discord use PATCH /guilds/{guild_id}/roles with array of {id, position}
        try:
            # Make direct API request since fluxer-py might not have this method
            from fluxer.http import Route

            await self.fluxer_http.request(
                Route("PATCH", "/guilds/{guild_id}/roles", guild_id=fluxer_guild_id),
                json=payload,
            )
            self.logger.log(f"✓ Reordered {len(payload)} roles")
        except Exception as e:
            self.logger.log(f"Failed to reorder roles: {e}", "WARN")
            self.logger.log(
                "(Roles were created but may not be in correct order)", "WARN"
            )

    async def _migrate_roles(
        self,
        roles: list[dict[str, Any]],
        fluxer_guild_id: str,
        existing_fluxer_roles: list[dict[str, Any]] | None = None,
    ):
        """Migrate roles from Discord to Fluxer."""
        self.logger.log("\n--- Migrating Roles ---")

        # Fetch existing roles if not provided
        if existing_fluxer_roles is None:
            existing_fluxer_roles = await self.fluxer_http.get_guild_roles(
                fluxer_guild_id
            )

        # Build a name-to-role map for existing roles
        existing_roles_by_name = {r["name"]: r for r in existing_fluxer_roles}

        # Fluxer role IDs in final hierarchy order, lowest rank first
        ordered_fluxer_role_ids: list[str] = []

        # Map @everyone role (it exists by default in Fluxer, so we just need to map IDs).
        # It is deliberately left out of the reorder payload: @everyone is pinned to
        # position 0 at the bottom of the hierarchy and cannot be moved.
        everyone_role = next((r for r in roles if r.get("name") == "@everyone"), None)
        if everyone_role:
            fluxer_everyone = existing_roles_by_name.get("@everyone")
            if fluxer_everyone:
                self.role_id_map[everyone_role["id"]] = fluxer_everyone["id"]
                self.logger.log(
                    f"Mapped @everyone role (Discord: {everyone_role['id']} → Fluxer: {fluxer_everyone['id']})"
                )

        # Skip @everyone role from creation (it already exists)
        roles = [r for r in roles if r.get("name") != "@everyone"]

        # Managed roles belong to bots/integrations; Fluxer cannot recreate them and
        # copying them as plain roles would distort the hierarchy.
        managed_roles = [r for r in roles if r.get("managed")]
        if managed_roles:
            for role in managed_roles:
                self.logger.log(
                    f"Skipping managed (bot/integration) role: {role['name']}"
                )
            roles = [r for r in roles if not r.get("managed")]

        # Create from the bottom of the hierarchy up. Discord's `position` is sparse,
        # so it is used only to establish relative order, never sent to Fluxer.
        # Ties are broken by role ID (older role first) to match Discord's own ordering.
        #
        # Roles are always created at position 1 by Fluxer; the ordering is applied
        # by the reorder PATCH below. That reorder therefore runs even if this loop
        # is interrupted, otherwise every role would be left tied at position 1 and
        # the hierarchy would show in creation order (newest first).
        interrupted = False
        try:
            for role in sorted(
                roles, key=lambda r: (r.get("position", 0), int(r["id"]))
            ):
                role_name = role["name"]
                role_id = role["id"]
                color = role.get("color", 0)
                permissions = int(role.get("permissions", 0))
                hoist = role.get("hoist", False)
                mentionable = role.get("mentionable", False)

                # Check if role already exists (partial sync)
                if role_name in existing_roles_by_name:
                    existing_role = existing_roles_by_name[role_name]
                    fluxer_role_id = existing_role["id"]
                    self.role_id_map[role_id] = fluxer_role_id
                    self.logger.log(f"Skipping role (already exists): {role_name}")
                    self.logger.log(
                        f"  Mapped: Discord {role_id} → Fluxer {fluxer_role_id}"
                    )

                    # Keep in hierarchy order for reordering
                    ordered_fluxer_role_ids.append(fluxer_role_id)
                    continue

                self.logger.log(f"Creating role: {role_name}")
                self.logger.log(f"  Color: #{color:06x}")
                self.logger.log(f"  Permissions: {permissions}")

                try:
                    fluxer_role = await self.fluxer_http.create_guild_role(
                        fluxer_guild_id,
                        name=role_name,
                        permissions=permissions,
                        color=color,
                        hoist=hoist,
                        mentionable=mentionable,
                    )

                    fluxer_role_id = fluxer_role["id"]
                    self.role_id_map[role_id] = fluxer_role_id
                    self.logger.log(f"  ✓ Role created with ID: {fluxer_role_id}")

                    # Fluxer's create endpoint ignores hoist/mentionable, so they
                    # have to be applied with a follow-up edit.
                    await self._apply_role_flags(
                        fluxer_guild_id,
                        fluxer_role_id,
                        role_name,
                        hoist=hoist,
                        mentionable=mentionable,
                    )

                    # Keep in hierarchy order for reordering
                    ordered_fluxer_role_ids.append(fluxer_role_id)

                    # Pacing is handled by RateLimitedClient before each create

                except Exception as e:
                    self.logger.log(f"  Failed to create role '{role_name}': {e}", "WARN")

        except KeyboardInterrupt:
            interrupted = True
            self.logger.log(
                "Interrupted - applying role order for the roles created so far...",
                "WARN",
            )

        self.logger.log(f"✓ Migrated {len(ordered_fluxer_role_ids)} roles")

        # Partial sync: roles that exist only in Fluxer are not part of the Discord
        # hierarchy. Keep them above the migrated roles (preserving their relative
        # order) so they are not left sharing positions with the roles we just placed.
        migrated_ids = set(ordered_fluxer_role_ids)
        fluxer_only_roles = [
            r
            for r in existing_fluxer_roles
            if r["id"] not in migrated_ids and r.get("name") != "@everyone"
        ]
        if fluxer_only_roles:
            ordered_fluxer_role_ids += [
                r["id"]
                for r in sorted(fluxer_only_roles, key=lambda r: r.get("position", 0))
            ]

        # Reorder all roles to match Discord hierarchy
        if ordered_fluxer_role_ids:
            await self._reorder_roles(fluxer_guild_id, ordered_fluxer_role_ids)

        # Ctrl+C still cancels the migration, but only after the roles that were
        # created have been put in the right order.
        if interrupted:
            raise KeyboardInterrupt

    async def _apply_channel_permissions(
        self, discord_channel: dict[str, Any], fluxer_channel_id: str
    ):
        """Apply permission overwrites to a Fluxer channel.

        Args:
            discord_channel: Discord channel data with permission_overwrites
            fluxer_channel_id: ID of the Fluxer channel to apply permissions to
        """
        permission_overwrites = discord_channel.get("permission_overwrites", [])

        if not permission_overwrites:
            return

        for overwrite in permission_overwrites:
            # Get the target ID and type (role or member)
            target_id = overwrite.get("id")
            target_type = overwrite.get("type")  # 0 = role, 1 = member

            # Only handle role permissions for now (member permissions would need user ID mapping)
            if target_type != 0:
                continue

            # Map Discord role ID to Fluxer role ID
            fluxer_role_id = self.role_id_map.get(target_id)
            if not fluxer_role_id:
                continue

            allow = int(overwrite.get("allow", 0))
            deny = int(overwrite.get("deny", 0))

            try:
                await self.fluxer_http.edit_channel_permissions(
                    fluxer_channel_id,
                    fluxer_role_id,
                    allow=allow,
                    deny=deny,
                    type=0,  # Role
                )
                self.logger.log(f"    Applied permission overwrite for role")
            except Exception as e:
                self.logger.log(f"    Failed to apply permission: {e}", "WARN")

    async def _migrate_channels(
        self,
        channels: list[dict[str, Any]],
        fluxer_guild_id: str,
        migrate_permissions: bool = True,
        existing_fluxer_channels: list[dict[str, Any]] | None = None,
    ):
        """Migrate channels from Discord to Fluxer."""
        self.logger.log("\n--- Migrating Channels ---")

        # Build name-to-channel map for existing channels (if doing partial sync)
        existing_channels_by_name = {}
        if existing_fluxer_channels:
            existing_channels_by_name = {
                ch["name"]: ch for ch in existing_fluxer_channels
            }

        # Channel types: 0=text, 2=voice, 4=category, 5=announcement, 13=stage, 15=forum
        # First pass: Create categories
        categories = [ch for ch in channels if ch.get("type") == 4]

        for category in sorted(categories, key=lambda c: c.get("position", 0)):
            category_name = category["name"]
            category_id = category["id"]

            # Check if category already exists (partial sync)
            if category_name in existing_channels_by_name:
                existing_category = existing_channels_by_name[category_name]
                self.category_id_map[category_id] = existing_category["id"]
                self.logger.log(f"Skipping category (already exists): {category_name}")
                self.logger.log(
                    f"  Mapped: Discord {category_id} → Fluxer {existing_category['id']}"
                )

                # Apply permissions if enabled (update existing channel permissions)
                if migrate_permissions:
                    await self._apply_channel_permissions(
                        category, existing_category["id"]
                    )

                continue

            self.logger.log(f"Creating category: {category_name}")

            try:
                fluxer_category = await self.fluxer_http.create_guild_channel(
                    fluxer_guild_id,
                    name=category_name,
                    type=4,  # Category type
                    position=category.get("position", 0),
                )

                fluxer_category_id = fluxer_category["id"]
                self.category_id_map[category_id] = fluxer_category_id
                self.logger.log(f"  ✓ Category created")

                # Apply permissions if enabled
                if migrate_permissions:
                    await self._apply_channel_permissions(category, fluxer_category_id)

                # Pacing is handled by RateLimitedClient before each create

            except Exception as e:
                self.logger.log(
                    f"  Failed to create category '{category_name}': {e}", "WARN"
                )

        # Second pass: Create channels
        channel_count = 0

        for channel in sorted(channels, key=lambda c: c.get("position", 0)):
            channel_type = channel.get("type", 0)
            channel_name = channel.get("name", "unnamed")
            channel_id = channel["id"]

            # Skip categories (already done)
            if channel_type == 4:
                continue

            parent_id = None
            if channel.get("parent_id"):
                parent_id = self.category_id_map.get(channel["parent_id"])

            # Text channels (0) or Announcement channels (5)
            if channel_type in [0, 5]:
                # Check if channel already exists (partial sync)
                if channel_name in existing_channels_by_name:
                    existing_channel = existing_channels_by_name[channel_name]
                    self.channel_id_map[channel_id] = existing_channel["id"]
                    self.logger.log(
                        f"Skipping text channel (already exists): #{channel_name}"
                    )
                    self.logger.log(
                        f"  Mapped: Discord {channel_id} → Fluxer {existing_channel['id']}"
                    )

                    # Apply permissions if enabled (update existing channel permissions)
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, existing_channel["id"]
                        )

                    channel_count += 1
                    continue

                self.logger.log(f"Creating text channel: #{channel_name}")

                try:
                    fluxer_channel = await self.fluxer_http.create_guild_channel(
                        fluxer_guild_id,
                        name=channel_name,
                        type=0,  # Text channel
                        topic=channel.get("topic"),
                        nsfw=channel.get("nsfw", False),
                        position=channel.get("position", 0),
                        parent_id=parent_id,
                    )
                    fluxer_channel_id = fluxer_channel["id"]
                    self.channel_id_map[channel_id] = fluxer_channel_id
                    self.logger.log(f"  ✓ Text channel created")

                    # Apply permissions if enabled
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, fluxer_channel_id
                        )

                    channel_count += 1
                except Exception as e:
                    self.logger.log(
                        f"  Failed to create text channel '{channel_name}': {e}", "WARN"
                    )

            # Voice channels (2)
            elif channel_type == 2:
                # Check if channel already exists (partial sync)
                if channel_name in existing_channels_by_name:
                    existing_channel = existing_channels_by_name[channel_name]
                    self.channel_id_map[channel_id] = existing_channel["id"]
                    self.logger.log(
                        f"Skipping voice channel (already exists): {channel_name}"
                    )
                    self.logger.log(
                        f"  Mapped: Discord {channel_id} → Fluxer {existing_channel['id']}"
                    )

                    # Apply permissions if enabled (update existing channel permissions)
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, existing_channel["id"]
                        )

                    channel_count += 1
                    continue

                self.logger.log(f"Creating voice channel: {channel_name}")

                try:
                    fluxer_channel = await self.fluxer_http.create_guild_channel(
                        fluxer_guild_id,
                        name=channel_name,
                        type=2,  # Voice channel
                        bitrate=channel.get("bitrate", 64000),
                        user_limit=channel.get("user_limit", 0),
                        position=channel.get("position", 0),
                        parent_id=parent_id,
                    )
                    fluxer_channel_id = fluxer_channel["id"]
                    self.channel_id_map[channel_id] = fluxer_channel_id
                    self.logger.log(f"  ✓ Voice channel created")

                    # Apply permissions if enabled
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, fluxer_channel_id
                        )

                    channel_count += 1
                except Exception as e:
                    self.logger.log(
                        f"  Failed to create voice channel '{channel_name}': {e}",
                        "WARN",
                    )

            # Forum channels (15) -> Convert to text channel
            elif channel_type == 15:
                # Check if channel already exists (partial sync)
                if channel_name in existing_channels_by_name:
                    existing_channel = existing_channels_by_name[channel_name]
                    self.channel_id_map[channel_id] = existing_channel["id"]
                    self.logger.log(
                        f"Skipping forum channel (already exists): {channel_name}"
                    )
                    self.logger.log(
                        f"  Mapped: Discord {channel_id} → Fluxer {existing_channel['id']}"
                    )

                    # Apply permissions if enabled (update existing channel permissions)
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, existing_channel["id"]
                        )

                    channel_count += 1
                    continue

                self.logger.log(f"Converting forum channel to text: {channel_name}")
                self.logger.log_unsupported(
                    "Forum channel", f"Converting '{channel_name}' to text channel"
                )

                try:
                    topic = f"Converted from forum. {channel.get('topic', '')}".strip()
                    fluxer_channel = await self.fluxer_http.create_guild_channel(
                        fluxer_guild_id,
                        name=channel_name,
                        type=0,  # Text channel
                        topic=topic,
                        nsfw=channel.get("nsfw", False),
                        position=channel.get("position", 0),
                        parent_id=parent_id,
                    )
                    fluxer_channel_id = fluxer_channel["id"]
                    self.channel_id_map[channel_id] = fluxer_channel_id
                    self.logger.log(f"  ✓ Converted to text channel")

                    # Apply permissions if enabled
                    if migrate_permissions:
                        await self._apply_channel_permissions(
                            channel, fluxer_channel_id
                        )

                    channel_count += 1
                except Exception as e:
                    self.logger.log(
                        f"  Failed to convert forum '{channel_name}': {e}", "WARN"
                    )

            # Stage channels (13)
            elif channel_type == 13:
                self.logger.log(f"Skipping stage channel: {channel_name}")
                self.logger.log_unsupported(
                    "Stage channel", f"Skipped '{channel_name}'"
                )

            # Threads (10, 11, 12)
            elif channel_type in [10, 11, 12]:
                self.logger.log(f"Skipping thread: {channel_name}")
                self.logger.log_unsupported("Thread", f"Skipped '{channel_name}'")

            # Other types
            else:
                self.logger.log(f"Unknown channel type {channel_type}: {channel_name}")
                self.logger.log_unsupported(
                    f"Channel type: {channel_type}", f"Skipped '{channel_name}'"
                )

        self.logger.log(f"✓ Migrated {channel_count} channels")

    async def _migrate_emojis(
        self,
        emojis: list[dict[str, Any]],
        discord_guild_id: str,
        fluxer_guild_id: str,
        existing_fluxer_emojis: list[dict[str, Any]] | None = None,
        save_to_disk: bool = False,
        base_path: str = "./migrated-data",
    ):
        """Migrate emojis from Discord to Fluxer."""
        self.logger.log("\n--- Migrating Emojis ---")

        if not emojis:
            self.logger.log("No emojis to migrate")
            return

        emoji_folder = Path(base_path) / discord_guild_id / "emojis"
        if save_to_disk:
            emoji_folder.mkdir(parents=True, exist_ok=True)
            self.logger.log(f"Saving emojis to: {emoji_folder.absolute()}")

        # Build name-to-emoji map for existing emojis (if doing partial sync)
        existing_emojis_by_name = {}
        if existing_fluxer_emojis:
            existing_emojis_by_name = {e["name"]: e for e in existing_fluxer_emojis}

        emoji_count = 0

        for emoji in emojis:
            emoji_name = emoji.get("name", "unnamed")
            emoji_id = emoji["id"]
            animated = emoji.get("animated", False)
            emoji_ext = "gif" if animated else "png"

            # Check if emoji already exists (partial sync)
            if emoji_name in existing_emojis_by_name:
                self.logger.log(f"Skipping emoji (already exists): :{emoji_name}:")
                emoji_count += 1
                continue

            # Build emoji URL
            emoji_ext = "webp" if animated else "png"
            emoji_url = (
                f"https://cdn.discordapp.com/emojis/{emoji_id}.{emoji_ext}?size=128"
            )
            if animated:
                emoji_url += "&animated=true"

            self.logger.log(
                f"Migrating emoji: :{emoji_name}: {'(animated)' if animated else ''}"
            )

            try:
                # Download emoji image
                emoji_data = await self._download_image(emoji_url)

                if not emoji_data:
                    self.logger.log(f"  Failed to download emoji", "WARN")
                    continue

                # Save to Disk
                if save_to_disk:
                    try:
                        filename = f"{emoji_name}.{emoji_ext}"
                        filepath = emoji_folder / filename
                        with open(filepath, "wb") as f:
                            f.write(emoji_data)
                        self.logger.log(f" ✓ Saved to disk: {filename}")
                    except Exception as e:
                        self.logger.log(f" Failed to save to disk: {e}", "WARN")

                # Upload to Fluxer
                await self.fluxer_http.create_guild_emoji(
                    fluxer_guild_id, name=emoji_name, image=emoji_data
                )

                self.logger.log(f"  ✓ Emoji created")
                emoji_count += 1

                # Emoji/sticker creation is not rate limited server-side;
                # a short delay keeps uploads from saturating the connection.
                await asyncio.sleep(0.3)

            except Exception as e:
                self.logger.log(f"  Failed to create emoji '{emoji_name}': {e}", "WARN")

        self.logger.log(f"✓ Migrated {emoji_count} emojis")

    async def _migrate_stickers(
        self,
        stickers: list[dict[str, Any]],
        discord_guild_id: str,
        fluxer_guild_id: str,
        existing_fluxer_stickers: list[dict[str, Any]] | None = None,
        save_to_disk: bool = False,
        base_path: str = "./migrated-data",
    ):
        """Migrate stickers from Discord to Fluxer."""
        self.logger.log("\n--- Migrating Stickers ---")

        if not stickers:
            self.logger.log("No stickers to migrate")
            return

        sticker_folder = Path(base_path) / discord_guild_id / "stickers"
        if save_to_disk:
            sticker_folder.mkdir(parents=True, exist_ok=True)
            self.logger.log(f"Saving stickers to: {sticker_folder.absolute()}")

        # Build name-to-sticker map for existing stickers (if doing partial sync)
        existing_stickers_by_name = {}
        if existing_fluxer_stickers:
            existing_stickers_by_name = {e["name"]: e for e in existing_fluxer_stickers}

        sticker_count = 0

        for sticker in stickers:
            sticker_name = sticker.get("name", "unnamed")
            sticker_id = sticker["id"]
            # Stickers use format_type instead of animated like emojis, 1 = PNG, 2 = APNG, 3 = LOTTIE, 4 = GIF.
            format_type = sticker.get("format_type", 1)
            # Set an ignore for LOTTIE as there is no current Fluxer support for it.
            if format_type == 3:
                self.logger.log(
                    f"Skipping Lottie sticker (JSON format not supported): :{sticker_name}:",
                    "WARN",
                )
                continue
            sticker_ext = "gif" if format_type == 4 else "png"
            is_animated = format_type in (2, 4)  # APNG or GIF

            # Check if sticker already exists (partial sync)
            if sticker_name in existing_stickers_by_name:
                self.logger.log(f"Skipping sticker (already exists): :{sticker_name}:")
                sticker_count += 1
                continue

            # Build sticker URL
            sticker_url = f"https://media.discordapp.net/stickers/{sticker_id}.{sticker_ext}?size=160"

            self.logger.log(
                f"Migrating sticker: :{sticker_name}: {'(animated)' if is_animated else ''}"
            )

            try:
                # Download sticker image
                sticker_data = await self._download_image(sticker_url)

                if not sticker_data:
                    self.logger.log(f"  Failed to download sticker", "WARN")
                    continue

                # Convert APNG to GIF (Fluxer doesn't support APNG properly)
                if format_type == 2:
                    gif_data = self._apng_to_gif(sticker_data)
                    if gif_data:
                        sticker_data = gif_data
                        sticker_ext = "gif"
                    else:
                        self.logger.log(
                            f"  Skipping APNG sticker (conversion failed): :{sticker_name}:",
                            "WARN",
                        )
                        continue

                # Save to Disk
                if save_to_disk:
                    try:
                        filename = f"{sticker_name}.{sticker_ext}"
                        filepath = sticker_folder / filename
                        with open(filepath, "wb") as f:
                            f.write(sticker_data)
                        self.logger.log(f" ✓ Saved to disk: {filename}")
                    except Exception as e:
                        self.logger.log(f" Failed to save to disk: {e}", "WARN")

                # Upload to Fluxer
                await self.fluxer_http.create_guild_sticker(
                    fluxer_guild_id, name=sticker_name, image=sticker_data
                )

                self.logger.log(f"  ✓ Sticker created")
                sticker_count += 1

                # Emoji/sticker creation is not rate limited server-side;
                # a short delay keeps uploads from saturating the connection.
                await asyncio.sleep(0.3)

            except Exception as e:
                self.logger.log(
                    f"  Failed to create sticker '{sticker_name}': {e}", "WARN"
                )

        self.logger.log(f"✓ Migrated {sticker_count} stickers")
