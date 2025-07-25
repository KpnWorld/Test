import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, cast, Union
import math
import random
import time
import logging
from utils.features import FeatureType

class LeaderboardView(discord.ui.View):
    def __init__(self, cog, page: int, has_next: bool):
        super().__init__(timeout=180)
        self.cog = cog
        self.current_page = page
        
        # Add buttons with proper button classes
        prev_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.gray,
            disabled=(page <= 1),
            custom_id="prev"
        )
        prev_button.callback = self.previous_callback
        self.add_item(prev_button)
        
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.gray,
            disabled=not has_next,
            custom_id="next"
        )
        next_button.callback = self.next_callback
        self.add_item(next_button)
    
    async def previous_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.display_leaderboard(interaction, self.current_page - 1)
    
    async def next_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.display_leaderboard(interaction, self.current_page + 1)

class LevelingCog(commands.Cog):
    """Cog for managing the leveling system"""
    
    def __init__(self, bot):
        self.bot = bot
        self.log = logging.getLogger("cogs.leveling")  # Use standard logging
        self.xp_cooldowns = {}
        
    def _calculate_level(self, xp: int) -> int:
        """Calculate level from XP amount"""
        return int(math.sqrt(xp) // 10)
        
    def _calculate_xp_for_level(self, level: int) -> int:
        """Calculate XP needed for a specific level"""
        return (level * 10) ** 2
    
    async def _check_leveling_enabled(self, guild_id: int) -> bool:
        """Check if leveling is enabled for guild"""
        settings = await self.bot.features.get_feature_settings(guild_id, FeatureType.LEVELING)
        return settings['enabled']

    @app_commands.command(name="level", description="View your level and XP.")
    @app_commands.guild_only()
    @app_commands.describe(user="User to check level for (defaults to yourself)")
    async def level(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        if not await self._check_leveling_enabled(interaction.guild.id):
            return await interaction.response.send_message(
                "Leveling is not enabled on this server!", ephemeral=True
            )

        """View level and XP stats."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        target = user or cast(discord.Member, interaction.user)  # Cast to Member since we know it's in guild
        
        try:
            # Get user's XP data
            xp_data = await self.bot.db.get_user_xp(interaction.guild.id, target.id)
            
            if not xp_data:
                return await interaction.response.send_message(
                    "❌ No XP data found for this user!" if user else "❌ You haven't earned any XP yet!",
                    ephemeral=True
                )
            
            current_xp = xp_data['xp']
            current_level = xp_data['level']
            next_level_xp = self._calculate_xp_for_level(current_level + 1)
            
            # Create progress bar
            progress = current_xp - self._calculate_xp_for_level(current_level)
            total_needed = next_level_xp - self._calculate_xp_for_level(current_level)
            progress_percentage = (progress / total_needed) * 100
            bars_filled = int((progress_percentage / 100) * 10)
            progress_bar = f"{'█' * bars_filled}{'░' * (10 - bars_filled)}"
            
            embed = discord.Embed(
                title=f"📊 Level Stats for {target}",
                color=target.color if isinstance(target, discord.Member) else discord.Color.blue()
            )
            
            # Add basic level info
            embed.add_field(
                name=f"Level {current_level}",
                value=f"XP: {current_xp:,}/{next_level_xp:,}\n{progress_bar} {progress_percentage:.1f}%",
                inline=False
            )
            
            # Add last message XP gain info if available
            if 'last_xp_gain' in xp_data and 'last_message' in xp_data:
                embed.add_field(
                    name="Last XP Gain",
                    value=f"+{xp_data['last_xp_gain']} XP\n```{xp_data['last_message']}```",
                    inline=False
                )
            
            # Add rank info
            leaderboard = await self.bot.db.get_leaderboard(interaction.guild.id)
            rank = next((i + 1 for i, entry in enumerate(leaderboard) if entry['user_id'] == target.id), None)
            
            if rank:
                embed.add_field(name="Rank", value=f"#{rank}", inline=True)
            
            embed.set_thumbnail(url=target.display_avatar.url)
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            self.log.error(f"Error in level command: {e}", exc_info=True)
            await interaction.response.send_message("❌ An unexpected error occurred.", ephemeral=True)
    
    async def display_leaderboard(self, interaction: discord.Interaction, page: int = 1):
        """Handle leaderboard display logic."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
            
        if page < 1:
            return await interaction.response.send_message("Page number must be 1 or higher!", ephemeral=True)
            
        try:
            per_page = 10
            offset = (page - 1) * per_page
            
            # Get current page entries
            entries = await self.bot.db.get_leaderboard_page(interaction.guild.id, per_page + 1, offset)
            
            if not entries:
                return await interaction.response.send_message(
                    "No XP data found!" if page == 1 else "No more entries to display!",
                    ephemeral=True
                )
            
            # Check if there's a next page
            has_next = len(entries) > per_page
            if has_next:
                entries = entries[:per_page]
            
            embed = discord.Embed(
                title=f"🏆 XP Leaderboard for {interaction.guild.name}",
                color=discord.Color.gold()
            )
            
            for i, entry in enumerate(entries, start=offset + 1):
                member = interaction.guild.get_member(entry['user_id'])
                if member:
                    embed.add_field(
                        name=f"#{i} {member}",
                        value=f"Level {entry['level']} • {entry['xp']:,} XP",
                        inline=False
                    )
            
            embed.set_footer(text=f"Page {page}")
            
            # Create and send view
            view = LeaderboardView(self, page, has_next)
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, view=view)
            else:
                await interaction.response.send_message(embed=embed, view=view)
                
        except Exception as e:
            error_msg = "❌ An error occurred: " + str(e)
            if interaction.response.is_done():
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)

    @app_commands.command(name="leaderboard")
    @app_commands.guild_only()
    @app_commands.describe(page="Page number of the leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, page: int = 1):
        """View the server's XP leaderboard."""
        await self.display_leaderboard(interaction, page)

    levelconfig = app_commands.Group(name="levelconfig", description="Configure leveling system.")
    
    @levelconfig.command(name="cooldown", description="Set XP cooldown time.")
    @app_commands.describe(seconds="Cooldown time in seconds (0-3600)")
    async def set_cooldown(self, interaction: discord.Interaction, seconds: int):
        """Set the cooldown between XP gains."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        member = cast(discord.Member, interaction.user)
        if not member.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need the Manage Server permission to use this command!", ephemeral=True)
            
        if seconds < 0 or seconds > 3600:  # 1 hour max
            return await interaction.response.send_message("Cooldown must be between 0 and 3600 seconds!", ephemeral=True)
            
        try:
            # Get current feature settings
            feature_settings = await self.bot.db.get_feature_settings(interaction.guild.id, "leveling")
            if not feature_settings or not feature_settings['enabled']:
                return await interaction.response.send_message("The leveling system is currently disabled!", ephemeral=True)

            # Get current options or use defaults
            options = feature_settings.get('options', {})
            options['cooldown'] = seconds
            options['min_xp'] = options.get('min_xp', 15)
            options['max_xp'] = options.get('max_xp', 25)
            options['dm_notifications'] = options.get('dm_notifications', True)

            # Update feature settings
            await self.bot.db.set_feature_settings(interaction.guild.id, "leveling", True, options)
            
            await interaction.response.send_message(f"✅ XP cooldown set to {seconds} seconds.")
            
        except Exception as e:
            await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)
    
    @levelconfig.command(name="xprange", description="Set XP min/max range.")
    @app_commands.describe(
        min_xp="Minimum XP per message (1-100)",
        max_xp="Maximum XP per message (1-100)"
    )
    async def set_xprange(self, interaction: discord.Interaction, min_xp: int, max_xp: int):
        """Set the XP range for messages."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        member = cast(discord.Member, interaction.user)
        if not member.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need the Manage Server permission to use this command!", ephemeral=True)
            
        if min_xp < 1 or min_xp > 100 or max_xp < 1 or max_xp > 100:
            return await interaction.response.send_message("XP values must be between 1 and 100!", ephemeral=True)
            
        if min_xp > max_xp:
            return await interaction.response.send_message("Minimum XP cannot be greater than maximum XP!", ephemeral=True)
            
        try:
            # Get current feature settings
            feature_settings = await self.bot.db.get_feature_settings(interaction.guild.id, "leveling")
            if not feature_settings or not feature_settings['enabled']:
                return await interaction.response.send_message("The leveling system is currently disabled!", ephemeral=True)
                
            # Get current options or use defaults
            options = feature_settings.get('options', {})
            options['min_xp'] = min_xp
            options['max_xp'] = max_xp
            options['cooldown'] = options.get('cooldown', 60)
            options['dm_notifications'] = options.get('dm_notifications', True)
            
            # Update feature settings
            await self.bot.db.set_feature_settings(interaction.guild.id, "leveling", True, options)
            
            await interaction.response.send_message(f"✅ XP range set to {min_xp}-{max_xp} per message.")
            
        except Exception as e:
            await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)
    
    @levelconfig.command(name="togglenotifications", description="Toggle level-up DMs.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle_notifications(self, interaction: discord.Interaction):
        """Toggle level-up DM notifications."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        member = cast(discord.Member, interaction.user)
        if not member.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need the Manage Server permission to use this command!", ephemeral=True)

        try:
            # Get current feature settings
            feature_settings = await self.bot.db.get_feature_settings(interaction.guild.id, "leveling")
            if not feature_settings or not feature_settings['enabled']:
                return await interaction.response.send_message("The leveling system is currently disabled!", ephemeral=True)
            
            # Get current options or use defaults
            options = feature_settings.get('options', {})
            options['dm_notifications'] = not options.get('dm_notifications', True)
            options['cooldown'] = options.get('cooldown', 60)
            options['min_xp'] = options.get('min_xp', 15)
            options['max_xp'] = options.get('max_xp', 25)
            
            # Update feature settings
            await self.bot.db.set_feature_settings(interaction.guild.id, "leveling", True, options)
            
            status = "enabled" if options['dm_notifications'] else "disabled"
            await interaction.response.send_message(f"✅ Level-up DM notifications have been {status}.")
            
        except Exception as e:
            await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)
    
    @levelconfig.command(name="addrole", description="Add a role reward for reaching a level.")
    @app_commands.guild_only()
    @app_commands.describe(level="Level to award the role at", role="Role to award")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def levelconfig_add_role(self, interaction: discord.Interaction, level: int, role: discord.Role):
        """Add a role reward for reaching a level."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
        
        guild_id = interaction.guild.id  # Safe to access after null check
        
        if not interaction.guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message("I don't have permission to manage roles!", ephemeral=True)
            
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("I can't manage this role as it's higher than my highest role!", ephemeral=True)
            
        if level < 1:
            return await interaction.response.send_message("Level must be at least 1!", ephemeral=True)
            
        try:
            await self.bot.db.set_level_role(interaction.guild.id, level, role.id)
            await interaction.response.send_message(f"✅ Set {role.mention} as the reward for reaching level {level}.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @levelconfig.command(name="removerole", description="Remove a level role reward.")
    @app_commands.guild_only()
    @app_commands.describe(level="Level to remove the role reward from")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def levelconfig_remove_role(self, interaction: discord.Interaction, level: int):
        """Remove a level role reward."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        try:
            await self.bot.db.delete_level_role(interaction.guild.id, level)
            await interaction.response.send_message(f"✅ Removed the role reward for level {level}.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @levelconfig.command(name="roles", description="List all level role rewards.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_roles=True)
    async def levelconfig_list_roles(self, interaction: discord.Interaction):
        """List all level role rewards."""
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)

        try:
            guild = interaction.guild
            roles = await self.bot.db.get_level_roles(guild.id)
            if not roles:
                return await interaction.response.send_message("No level role rewards set up!", ephemeral=True)
                
            embed = discord.Embed(
                title="Level Role Rewards",
                color=discord.Color.blue()
            )
            
            for role_data in sorted(roles, key=lambda x: x['level']):
                role = interaction.guild.get_role(role_data['role_id'])
                if role:
                    embed.add_field(
                        name=f"Level {role_data['level']}",
                        value=role.mention,
                        inline=False
                    )
            
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @levelconfig.command(name="toggle", description="Enable or disable the leveling system")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def toggle_leveling(self, interaction: discord.Interaction):
        """Toggle command moved to /config"""
        await interaction.response.send_message(
            "❌ This command has been moved to `/config setting:Toggle Leveling`",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self._should_track_xp(message):
            return

        try:
            if not message.guild:
                return

            # Check feature settings using FeatureManager
            settings = await self.bot.features.get_feature_settings(
                message.guild.id, 
                FeatureType.LEVELING
            )
            
            if not settings['enabled']:
                return

            # Get settings from options
            options = settings['options']
            cooldown = options.get('cooldown', 60)
            min_xp = options.get('min_xp', 15)
            max_xp = options.get('max_xp', 25)

            guild_id = message.guild.id
            user_id = message.author.id
            
            # Check cooldown using database method
            last_msg_time = await self.bot.db.get_xp_cooldown(guild_id, user_id)
            if last_msg_time:
                current_time = time.time()
                if current_time - last_msg_time < cooldown:
                    return

            # Calculate XP gain
            xp_gain = random.randint(min_xp, max_xp)
            
            # Get current XP data
            xp_data = await self.bot.db.get_user_xp(guild_id, user_id) or {
                'xp': 0,
                'level': 0
            }
            
            new_xp = xp_data['xp'] + xp_gain
            new_level = self._calculate_level(new_xp)
            
            # Update XP with message tracking
            await self.bot.db.update_user_xp_with_message(
                guild_id,
                user_id,
                new_xp,
                new_level,
                xp_gain,
                message.content[:100]  # Store first 100 chars
            )
            
            # Set cooldown
            await self.bot.db.set_xp_cooldown(guild_id, user_id, time.time())
            
            # Handle level up with feature settings check for DM notifications
            if new_level > xp_data['level'] and isinstance(message.author, discord.Member):
                await self._handle_level_up(message.guild, message.author, new_level)
                
        except Exception as e:
            self.log.error(f"Error handling XP gain: {e}", exc_info=True)

    def _should_track_xp(self, message: discord.Message) -> bool:
        """Check if message should award XP"""
        if not message.guild:
            return False
        if not isinstance(message.author, discord.Member):
            return False
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False
        return not message.author.bot

    async def _handle_level_up(self, guild: discord.Guild, member: discord.Member, new_level: int):
        """Handle level up rewards and notifications."""
        try:
            # Get feature settings for leveling to check DM notification preference
            feature_settings = await self.bot.db.get_feature_settings(guild.id, "leveling")
            if not feature_settings or not feature_settings['enabled']:
                return

            options = feature_settings['options']
            dm_notifications = options.get('dm_notifications', True)  # Default to True
            
            # Handle role rewards
            reward_roles = await self.bot.db.get_level_roles_for_level(guild.id, new_level)
            for role_id in reward_roles:
                role = guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Level {new_level} reward")
                    except discord.Forbidden:
                        self.log.warning(f"Cannot add role {role.id} to {member.id} - Missing permissions")

            # Send DM notification if enabled
            if dm_notifications:
                try:
                    embed = discord.Embed(
                        title="🎉 Level Up!",
                        description=f"You reached level {new_level} in {guild.name}!",
                        color=discord.Color.green()
                    )
                    await member.send(embed=embed)
                except discord.Forbidden:
                    pass  # Can't send DM to user
                    
        except Exception as e:
            self.log.error(f"Error handling level up: {e}", exc_info=True)

    @levelconfig.command(name="reset", description="Reset a user's XP and level.")
    @app_commands.guild_only()
    @app_commands.describe(user="User to reset XP for")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_xp(self, interaction: discord.Interaction, user: discord.Member):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
        
        guild_id = interaction.guild.id  # Safe to access after null check
        try:
            await self.bot.db.reset_user_xp(guild_id, user.id)
            await interaction.response.send_message(
                f"✅ Reset XP and level for {user.mention}.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    @levelconfig.command(name="setlevel", description="Set a user's level directly.")
    @app_commands.guild_only()
    @app_commands.describe(user="User to set level for", level="New level")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_user_level(self, interaction: discord.Interaction, user: discord.Member, level: int):
        if not interaction.guild:
            return await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
            
        guild_id = interaction.guild.id  # Safe to access after null check
        if level < 0:
            return await interaction.response.send_message("Level cannot be negative!", ephemeral=True)
        
        try:
            xp = self._calculate_xp_for_level(level)
            await self.bot.db.update_user_xp(guild_id, user.id, xp, level)
            await interaction.response.send_message(
                f"✅ Set {user.mention}'s level to {level}.", ephemeral=True
            )
            
            # Grant role rewards
            if interaction.guild:  # Extra check since we're calling another method
                await self._handle_level_up(interaction.guild, user, level)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(LevelingCog(bot))
