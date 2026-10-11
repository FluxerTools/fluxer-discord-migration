"""Discord to Fluxer Migration Tool

This tool helps you migrate your Discord profile and servers to Fluxer.
Uses user tokens for both Discord and Fluxer - no bots required!

Features:
- Migrate profile (display name, avatar, banner, bio, pronouns, colors)
- Migrate servers you own (channels, voice channels, roles)
- Skip unsupported features and log them
"""

from __future__ import annotations

import asyncio
from typing import Any
import inquirer
from inquirer.themes import BlueComposure

from utils import MigrationLogger, load_config, clean_token
from clients import DiscordHTTPClient, FluxerClient
from migrators import ProfileMigrator, ServerMigrator


class MigrationOrchestrator:
    """Main orchestrator for Discord to Fluxer migration."""

    def __init__(self):
        self.discord_http: DiscordHTTPClient | None = None
        self.fluxer_http: FluxerClient | None = None
        self.logger = MigrationLogger()
        self.discord_user: dict[str, Any] | None = None
        self.discord_guilds: list[dict[str, Any]] = []

    async def setup_discord(self, token: str):
        """Initialize Discord HTTP client."""
        # Clean the token
        token = clean_token(token)

        # Basic validation - just check it's not empty
        if not token or len(token) < 20:
            raise ValueError(
                "Token appears to be too short or empty.\n"
                "Make sure you copied the ENTIRE token from the Authorization header."
            )

        self.discord_http = DiscordHTTPClient(token)

        try:
            # Test connection and get user info
            self.discord_user = await self.discord_http.get_current_user()
            username = self.discord_user.get("username", "Unknown")
            discriminator = self.discord_user.get("discriminator")

            if discriminator and discriminator != "0":
                user_display = f"{username}#{discriminator}"
            else:
                user_display = username

            self.logger.log(f"Connected to Discord as {user_display}")

            # Fetch guilds
            self.discord_guilds = await self.discord_http.get_guilds()
            self.logger.log(f"Found {len(self.discord_guilds)} Discord servers")

        except ValueError as e:
            raise ValueError(
                f"Discord connection failed: {e}\n\n"
                "Your token may be invalid or expired.\n"
                "How to get a fresh token:\n"
                "  1. Open Discord in browser (discord.com/app)\n"
                "  2. Press F12 → Network tab\n"
                "  3. Press F5 to refresh\n"
                "  4. Type 'api' in filter\n"
                "  5. Click any request → Headers → Look for 'authorization:'\n"
                "  6. Copy the ENTIRE value after 'authorization:' or 'Bearer '"
            )
        except Exception as e:
            raise ValueError(f"Unexpected error connecting to Discord: {e}")

    async def setup_fluxer(self, token: str):
        """Initialize Fluxer HTTP client with user token."""
        # Clean the token
        token = clean_token(token)

        self.fluxer_http = FluxerClient(token)

        # Test connection
        try:
            user_data = await self.fluxer_http.get_current_user()
            self.logger.log(f"Connected to Fluxer as {user_data.get('username', 'Unknown')}")
        except Exception as e:
            self.logger.log(f"Failed to connect to Fluxer: {e}", "ERROR")
            raise ValueError(
                "Fluxer connection failed. Your token may be invalid or expired.\n"
                "Get a fresh Fluxer token the same way as Discord:\n"
                "  1. Open Fluxer in browser\n"
                "  2. Press F12 → Network tab\n"
                "  3. Press F5 to refresh\n"
                "  4. Find any API request\n"
                "  5. Look for 'authorization:' header\n"
                "  6. Copy the entire token"
            )

    async def migrate_profile(self):
        """Migrate user profile from Discord to Fluxer."""
        migrator = ProfileMigrator(self.discord_user, self.discord_http, self.fluxer_http, self.logger)
        return await migrator.migrate()

    def select_servers(self) -> list[dict[str, Any]]:
        """Interactive server selection with checkbox interface."""
        if not self.discord_guilds or not self.discord_user:
            return []

        user_id = self.discord_user["id"]
        all_guilds = self.discord_guilds

        if not all_guilds:
            self.logger.log("You're not in any servers", "WARN")
            return []

        # Separate owned and non-owned servers
        owned_guilds = [g for g in all_guilds if (g.get("owner") or g.get("owner_id") == user_id)]

        print("\n" + "=" * 80)
        print("SERVER MIGRATION - SELECT SERVERS")
        print("=" * 80)
        print(f"\nYou're in {len(all_guilds)} server(s)")
        print(f"You own {len(owned_guilds)} server(s)")
        print("\nNote: You can copy ANY server you're in, even if you don't own it!")
        print("\nℹ️  Navigation: Use arrow keys to move, Enter to select, Ctrl+C to cancel")

        # Step 1: Ask what to show
        questions = [
            inquirer.List(
                'filter',
                message="Which servers do you want to see?",
                choices=[
                    ('All servers you\'re in', 'all'),
                    ('Only servers you own', 'owned'),
                    ('Cancel server migration', 'cancel')
                ],
            ),
        ]

        try:
            answers = inquirer.prompt(questions, theme=BlueComposure())

            if not answers or answers['filter'] == 'cancel':
                return []

            # Filter guilds based on selection
            guilds_to_show = owned_guilds if answers['filter'] == 'owned' else all_guilds

            if not guilds_to_show:
                print("\nNo servers found with the selected filter.")
                return []

            # Step 2: Build checkbox choices
            checkbox_choices = []
            for guild in guilds_to_show:
                member_count = guild.get("approximate_member_count", "?")
                owner_indicator = " [OWNER]" if (guild.get("owner") or guild.get("owner_id") == user_id) else ""
                display_name = f"{guild['name']} ({member_count} members){owner_indicator}"
                checkbox_choices.append((display_name, guild))

            # Step 3: Ask for selection method
            selection_questions = [
                inquirer.List(
                    'selection_mode',
                    message="How do you want to select servers?",
                    choices=[
                        ('Select servers manually (use arrow keys + space bar)', 'manual'),
                        ('Select ALL displayed servers', 'all'),
                        ('Cancel', 'cancel')
                    ],
                ),
            ]

            selection_answers = inquirer.prompt(selection_questions, theme=BlueComposure())

            if not selection_answers or selection_answers['selection_mode'] == 'cancel':
                return []

            # If "select all", return all displayed guilds
            if selection_answers['selection_mode'] == 'all':
                print(f"\n✓ Selected all {len(guilds_to_show)} server(s)")
                return guilds_to_show

            # Step 4: Show checkbox interface
            print("\n" + "=" * 80)
            print("SELECT SERVERS TO MIGRATE")
            print("=" * 80)
            print("Use arrow keys to navigate, SPACE to select/deselect, ENTER to confirm")
            print("Press Ctrl+C to cancel and go back")
            print("=" * 80 + "\n")

            checkbox_questions = [
                inquirer.Checkbox(
                    'servers',
                    message="Select the servers you want to migrate (space to toggle, enter to confirm)",
                    choices=checkbox_choices,
                ),
            ]

            checkbox_answers = inquirer.prompt(checkbox_questions, theme=BlueComposure())

            if not checkbox_answers or not checkbox_answers.get('servers'):
                print("\nNo servers selected.")
                return []

            selected_guilds = checkbox_answers['servers']
            print(f"\n✓ Selected {len(selected_guilds)} server(s)")
            return selected_guilds

        except KeyboardInterrupt:
            print("\n\nServer selection cancelled.")
            return []
        except Exception as e:
            # Fallback to old method if inquirer fails (e.g., on unsupported terminals)
            print(f"\n⚠ Interactive selection not available: {e}")
            print("Falling back to manual input method...\n")
            return self._select_servers_fallback(all_guilds, owned_guilds, user_id)

    def _select_servers_fallback(
        self,
        all_guilds: list[dict[str, Any]],
        owned_guilds: list[dict[str, Any]],
        user_id: str
    ) -> list[dict[str, Any]]:
        """Fallback server selection method (manual typing)."""
        print("\n" + "=" * 80)
        print("YOUR DISCORD SERVERS")
        print("=" * 80)

        for i, guild in enumerate(all_guilds, 1):
            member_count = guild.get("approximate_member_count", "?")
            owner_indicator = " [OWNER]" if (guild.get("owner") or guild.get("owner_id") == user_id) else ""
            print(f"  [{i}] {guild['name']} ({member_count} members){owner_indicator}")

        print("\nOptions:")
        print("  • Enter numbers separated by commas (e.g., 1,3,5)")
        print("  • Enter 'all' to select all servers")
        print("  • Enter 'owned' to select all servers you own")
        print("  • Enter 'cancel' to skip server migration")

        while True:
            choice = input("\nYour choice: ").strip().lower()

            if choice == "cancel":
                return []

            if choice == "all":
                return all_guilds

            if choice == "owned":
                return owned_guilds

            try:
                indices = [int(x.strip()) for x in choice.split(",")]
                selected = [all_guilds[i - 1] for i in indices if 1 <= i <= len(all_guilds)]

                if selected:
                    return selected
                else:
                    print("Invalid selection. Please try again.")
            except (ValueError, IndexError):
                print("Invalid input. Please try again.")

    def select_migration_options(self, discord_guild: dict[str, Any]) -> dict[str, bool] | None:
        """Ask user what to migrate for a specific server.

        Returns:
            Dict with migration options, or None if cancelled
        """
        print("\n" + "=" * 80)
        print(f"Migration Options for: {discord_guild['name']}")
        print("=" * 80)
        print("\nWhat would you like to migrate?")
        print("  [1] Everything (roles, channels, permissions, emojis, stickers)")
        print("  [2] Custom selection")
        print("  [3] Go back / Cancel")
        print("\nℹ️  Navigation: Enter a number, or press Ctrl+C to go back")

        choice = input("\nYour choice: ").strip()

        if choice == "1":
            save_to_disk = input("Save emojis and stickers to disk? (y/n) [default: n]: ").strip().lower() == 'y'
            return {
                'roles': True,
                'channels': True,
                'permissions': True,
                'emojis': True,
                'stickers': True,
                'save_to_disk': save_to_disk
            }

        elif choice == "2":
            print("\n" + "=" * 80)
            print("Custom Selection (y/n for each)")
            print("=" * 80)

            options = {}

            # Ask about each migration option
            migrate_roles = input("Migrate roles? (y/n): ").strip().lower() == 'y'
            options['roles'] = migrate_roles

            migrate_channels = input("Migrate channels? (y/n): ").strip().lower() == 'y'
            options['channels'] = migrate_channels

            if migrate_channels:
                migrate_permissions = input("Migrate channel permissions? (y/n): ").strip().lower() == 'y'
                options['permissions'] = migrate_permissions
            else:
                options['permissions'] = False

            migrate_emojis = input("Migrate emojis? (y/n): ").strip().lower() == 'y'
            options['emojis'] = migrate_emojis
            
            migrate_stickers = input("Migrate stickers? (y/n): ").strip().lower() == 'y'
            options['stickers'] = migrate_stickers

            save_to_disk = input("Save emojis and stickers to disk? (y/n): ").strip().lower() == 'y'
            options['save_to_disk'] = save_to_disk

            # Summary
            print("\n" + "=" * 80)
            print("Migration Summary:")
            print(f"  • Roles: {'✓' if options['roles'] else '✗'}")
            print(f"  • Channels: {'✓' if options['channels'] else '✗'}")
            print(f"  • Channel Permissions: {'✓' if options['permissions'] else '✗'}")
            print(f"  • Emojis: {'✓' if options['emojis'] else '✗'}")
            print(f"  • Stickers: {'✓' if options['stickers'] else '✗'}")
            print(f"  • Save to disk: {'✓' if options['save_to_disk'] else '✗'}")
            print("=" * 80)

            confirm = input("\nProceed with this configuration? (y/n): ").strip().lower()
            if confirm == 'y':
                return options
            else:
                return None

        elif choice == "3":
            return None

        else:
            print("Invalid choice. Cancelling.")
            return None

    async def select_fluxer_guild_for_migration(
        self,
        discord_guild: dict[str, Any]
    ) -> tuple[str | None, bool]:
        """Ask user if they want to create new or use existing Fluxer guild.

        Returns:
            Tuple of (Fluxer guild ID or None if creating new, partial_sync boolean)
        """
        print("\n" + "=" * 80)
        print(f"Discord Server: {discord_guild['name']}")
        print("=" * 80)
        print("\nWhere do you want to migrate this server?")
        print("  [1] Create a NEW Fluxer server")
        print("  [2] Add to an EXISTING Fluxer server (adds everything)")
        print("  [3] Add MISSING PARTS only (smart sync - matches by name)")
        print("  [4] Skip this server / Go back")
        print("\nℹ️  Navigation: Enter a number, or press Ctrl+C to go back")

        choice = input("\nYour choice: ").strip()

        if choice == "1":
            return (None, False)  # Create new, no partial sync

        elif choice in ["2", "3"]:
            # Both options need to select an existing server
            partial_sync = (choice == "3")

            if partial_sync:
                print("\n⚡ Smart Sync Mode Selected!")
                print("   Fetching your Fluxer servers...")
            else:
                print("\nFetching your Fluxer servers...")

            # Fetch Fluxer guilds
            try:
                fluxer_guilds = await self.fluxer_http.get_current_user_guilds()

                if not fluxer_guilds:
                    print("\nYou're not in any Fluxer servers yet!")
                    print("Creating a new server instead...")
                    return (None, False)

                print("\n" + "=" * 80)
                print("YOUR FLUXER SERVERS")
                print("=" * 80)

                for i, guild in enumerate(fluxer_guilds, 1):
                    owner_mark = " [OWNER]" if guild.get("owner") else ""
                    print(f"  [{i}] {guild['name']}{owner_mark}")

                while True:
                    guild_choice = input("\nSelect a server (or 'cancel'): ").strip()

                    if guild_choice.lower() == "cancel":
                        return ("SKIP", False)

                    try:
                        idx = int(guild_choice) - 1
                        if 0 <= idx < len(fluxer_guilds):
                            selected_guild = fluxer_guilds[idx]

                            if partial_sync:
                                print(f"\n✓ Will sync missing parts to: {selected_guild['name']}")
                                print("  ⚠ WARNING: This matches by exact name!")
                                print("  - Existing roles/channels/emojis/stickers will be SKIPPED")
                                print("  - Only MISSING items will be added")
                                print("  - Permissions will be updated for matched channels")
                                print("  - May result in duplicates if names don't match exactly")
                                print("\n  Starting smart sync in a moment...")
                            else:
                                print(f"\n✓ Will migrate into: {selected_guild['name']}")
                                print("  (Roles and channels will be ADDED, nothing will be deleted)")
                                print("\n  Starting migration in a moment...")

                            return (selected_guild["id"], partial_sync)
                        else:
                            print("Invalid selection. Try again.")
                    except ValueError:
                        print("Invalid input. Try again.")

            except Exception as e:
                error_msg = str(e)
                print(f"\n❌ Error fetching Fluxer servers: {error_msg}")

                # Check if it's a 500 error (server-side issue)
                if "500" in error_msg or "Server error" in error_msg:
                    print("\n🔴 Fluxer API is experiencing server issues (Error 500)")
                    print("   This is a problem on Fluxer's side, not yours.")
                    print("\n   What to do:")
                    print("   - Wait a few minutes and try again")
                    print("   - Check Fluxer's status page / Discord")
                    print("   - Or create a new server instead (option below)")
                else:
                    print("\nPossible causes:")
                    print("  - Your Fluxer token might be invalid/expired")
                    print("  - Network connection issue")

                retry = input("\nCreate new server instead? (y/n): ").strip().lower()
                if retry == 'y':
                    return (None, False)
                else:
                    return ("SKIP", False)

        elif choice == "4":
            return ("SKIP", False)

        else:
            print("Invalid choice. Skipping this server.")
            return ("SKIP", False)

    async def migrate_servers(self, servers: list[dict[str, Any]]):
        """Migrate multiple servers."""
        server_migrator = ServerMigrator(self.discord_http, self.fluxer_http, self.logger)

        for guild in servers:
            # Ask what to migrate for this guild
            migration_options = self.select_migration_options(guild)

            if migration_options is None:
                print(f"\nSkipping {guild['name']}")
                continue

            # Ask where to migrate this guild
            fluxer_guild_id, partial_sync = await self.select_fluxer_guild_for_migration(guild)

            if fluxer_guild_id == "SKIP":
                print(f"\nSkipping {guild['name']}")
                continue

            await server_migrator.migrate_server(
                guild,
                existing_fluxer_guild_id=fluxer_guild_id,
                migration_options=migration_options,
                partial_sync=partial_sync
            )
            # Small delay between servers
            await asyncio.sleep(1)

    async def select_fluxer_guild_for_reconfigure(self) -> str | None:
        """Pick which Fluxer server to reconfigure. Returns None to cancel."""
        print("\nFetching your Fluxer servers...")
        try:
            fluxer_guilds = await self.fluxer_http.get_current_user_guilds()
        except Exception as e:
            print(f"Failed to fetch Fluxer servers: {e}")
            return None

        if not fluxer_guilds:
            print("\nYou're not in any Fluxer servers yet!")
            return None

        print("\n" + "=" * 80)
        print("YOUR FLUXER SERVERS")
        print("=" * 80)
        for i, guild in enumerate(fluxer_guilds, 1):
            owner_mark = " [OWNER]" if guild.get("owner") else ""
            print(f"  [{i}] {guild['name']}{owner_mark}")

        while True:
            choice = input("\nSelect a server (or 'cancel'): ").strip()
            if choice.lower() == "cancel":
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(fluxer_guilds):
                    selected = fluxer_guilds[idx]
                    print(f"\n✓ Will reconfigure: {selected['name']}")
                    return selected["id"]
            except ValueError:
                pass
            print("Invalid choice. Try again.")

    def select_reconfigure_phases(self) -> dict[str, bool] | None:
        """Choose which parts of the server to reconfigure."""
        print("\nWhat should be reconfigured?")
        print("  [1] Everything (recommended)")
        print("  [2] Roles only")
        print("  [3] Channels & categories only")
        print("  [4] Emojis & stickers only")
        print("  [5] Choose individually")
        print("  [6] Cancel")

        choice = input("\nYour choice: ").strip()

        if choice == "1":
            return {
                "settings": True,
                "roles": True,
                "channels": True,
                "emojis": True,
                "stickers": True,
            }
        if choice == "2":
            return {"roles": True}
        if choice == "3":
            return {"channels": True}
        if choice == "4":
            return {"emojis": True, "stickers": True}
        if choice == "5":
            phases = {}
            for key, label in (
                ("settings", "Server settings (name, icon)"),
                ("roles", "Roles"),
                ("channels", "Channels & categories"),
                ("emojis", "Emojis"),
                ("stickers", "Stickers"),
            ):
                answer = input(f"  Include {label}? (y/n): ").strip().lower()
                phases[key] = answer in ("y", "yes")
            if not any(phases.values()):
                print("Nothing selected.")
                return None
            return phases
        return None

    async def reconfigure_servers(self, servers: list[dict[str, Any]]):
        """Sync an already-migrated Fluxer server back in line with Discord."""
        server_migrator = ServerMigrator(
            self.discord_http, self.fluxer_http, self.logger
        )

        for guild in servers:
            print("\n" + "=" * 80)
            print(f"Discord Server: {guild['name']}")
            print("=" * 80)
            print("\nReconfigure compares Discord against an existing Fluxer")
            print("server and can:")
            print("  - CREATE items that are missing on Fluxer")
            print("  - UPDATE items whose settings differ")
            print("  - DELETE Fluxer items that no longer exist in Discord")
            print("\n⚠ You are asked before every change, and deletions are")
            print("  always listed in full and confirmed separately.")

            phases = self.select_reconfigure_phases()
            if phases is None:
                print(f"\nSkipping {guild['name']}")
                continue

            fluxer_guild_id = await self.select_fluxer_guild_for_reconfigure()
            if fluxer_guild_id is None:
                print(f"\nSkipping {guild['name']}")
                continue

            await server_migrator.reconfigure_server(
                guild, fluxer_guild_id, phases=phases
            )
            await asyncio.sleep(1)

    async def run(self):
        """Main migration flow."""
        print("=" * 80)
        print(" " * 20 + "Discord to Fluxer Migration Tool")
        print("=" * 80)
        print()

        # Try to load from config
        config = load_config()
        discord_token = config["discord_token"]
        fluxer_token = config["fluxer_token"]

        # Prompt if not in config
        if not discord_token:
            print("This tool uses USER TOKENS (not bot tokens) for easy migration.")
            print()
            print("Enter your Discord user token:")
            print("  (How to get it: Discord Web → F12 → Network tab → Refresh →")
            print("   Find any 'api' request → Look for 'Authorization' header)")
            print("  (Or create config/config.json - see config/config.example.json)")
            discord_token = input("Discord Token: ").strip()

            if not discord_token:
                print("Error: Discord token is required")
                return
        else:
            print("✓ Loaded Discord token from config.json")

        if not fluxer_token:
            print("\nEnter your Fluxer user token:")
            print("  (Get it the same way from Fluxer web app)")
            print("  (Or create config/config.json - see config/config.example.json)")
            fluxer_token = input("Fluxer Token: ").strip()

            if not fluxer_token:
                print("Error: Fluxer token is required")
                return
        else:
            print("✓ Loaded Fluxer token from config.json")

        print("\n" + "=" * 80)
        print("Connecting to Discord and Fluxer...")
        print("=" * 80)

        try:
            # Setup clients
            await self.setup_discord(discord_token)
            await self.setup_fluxer(fluxer_token)

            # Main menu
            while True:
                print("\n" + "=" * 80)
                print("MIGRATION OPTIONS")
                print("=" * 80)
                print("  [1] Migrate Profile")
                print("  [2] Migrate Servers")
                print("  [3] Migrate Profile + Servers")
                print("  [4] Reconfigure / Sync an existing Fluxer server")
                print("  [5] Exit")
                print("\nℹ️  Navigation: Enter a number, or press Ctrl+C to exit")

                choice = input("\nYour choice: ").strip()

                if choice == "1":
                    await self.migrate_profile()

                elif choice == "2":
                    servers = self.select_servers()
                    if servers:
                        await self.migrate_servers(servers)

                elif choice == "3":
                    await self.migrate_profile()
                    print("\n")
                    servers = self.select_servers()
                    if servers:
                        await self.migrate_servers(servers)

                elif choice == "4":
                    servers = self.select_servers()
                    if servers:
                        await self.reconfigure_servers(servers)

                elif choice == "5":
                    break

                else:
                    print("Invalid choice. Please try again.")

        except Exception as e:
            self.logger.log(f"Migration failed: {e}", "ERROR")
            import traceback
            traceback.print_exc()

        finally:
            # Save log
            self.logger.save()

            # Cleanup
            if self.discord_http:
                await self.discord_http.close()
            if self.fluxer_http:
                await self.fluxer_http.close()

            # Give async cleanup time to finish (Windows fix)
            await asyncio.sleep(0.25)

            print("\n" + "=" * 80)
            print("Migration finished.")
            print("=" * 80)


async def main():
    """Entry point."""
    orchestrator = MigrationOrchestrator()
    await orchestrator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nMigration cancelled by user.")
