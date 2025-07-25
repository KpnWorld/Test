import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from datetime import datetime, timedelta
import logging
import asyncio

class ModerationCog(commands.Cog):
    """Cog for moderation commands"""
    
    def __init__(self, bot):
        self.bot = bot
        self.log = logging.getLogger("cogs.moderation")  # Use standard logging
    
    async def _check_mod_permissions(self, ctx_or_interaction) -> bool:
        """Check if user has mod permissions"""
        if isinstance(ctx_or_interaction, discord.Interaction):
            guild = ctx_or_interaction.guild
            user = ctx_or_interaction.user
        else:  # Context
            guild = ctx_or_interaction.guild
            user = ctx_or_interaction.author

        if not guild or not isinstance(user, discord.Member):
            return False
            
        # Check if user is admin or has manage server permission
        if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
            return True
            
        # Use proper database method
        mod_role_id = await self.bot.db.get_guild_setting(guild.id, "mod_role")
        if mod_role_id:            
            return discord.utils.get(user.roles, id=int(mod_role_id)) is not None
            
        return False
    
    async def _chunked_purge(self, channel: discord.TextChannel, amount: int) -> int:
        """Delete messages in chunks to handle rate limits better.
        Returns the number of messages actually deleted."""
        total_deleted = 0
        messages = []
        base_delay = 0.5  # Base delay between operations
        
        try:
            # Collect messages to delete
            async for message in channel.history(limit=amount):
                messages.append(message)
                
            if not messages:
                return 0
                
            # Use smaller chunks to better handle rate limits
            bulk_chunk_size = 50  # Maximum messages per bulk delete
            individual_chunk_size = 5  # Messages per individual deletion batch
            
            # First try bulk deletion in chunks
            current_chunk = []
            retry_delay = base_delay
            
            for message in messages:
                current_chunk.append(message)
                
                if len(current_chunk) >= bulk_chunk_size:
                    try:
                        await channel.delete_messages(current_chunk)
                        total_deleted += len(current_chunk)
                        current_chunk = []
                        await asyncio.sleep(1)  # Delay between bulk operations
                        retry_delay = base_delay  # Reset delay after successful operation
                    except discord.HTTPException as e:
                        if e.code == 50034:  # Messages too old
                            break  # Switch to individual deletion
                        elif e.code == 429:  # Rate limited
                            retry_after = getattr(e, 'retry_after', 1) + 0.5
                            self.log.warning(f"Rate limited during bulk delete, waiting {retry_after}s")
                            await asyncio.sleep(retry_after)
                            retry_delay = min(retry_delay * 2, 5)  # Exponential backoff
                            current_chunk = []  # Reset chunk after rate limit
                        else:
                            raise
            
            # Process any remaining messages in the bulk chunk
            if current_chunk:
                try:
                    await channel.delete_messages(current_chunk)
                    total_deleted += len(current_chunk)
                except discord.HTTPException:
                    pass  # Will handle remaining messages individually
            
            # Individual deletion for remaining or old messages
            remaining_messages = [m for m in messages if m not in current_chunk]
            current_chunk = []
            retry_delay = base_delay
            
            for message in remaining_messages:
                try:
                    await message.delete()
                    total_deleted += 1
                    await asyncio.sleep(retry_delay)  # Dynamic delay between deletions
                    
                except discord.NotFound:
                    continue
                except discord.HTTPException as e:
                    if e.code == 429:  # Rate limited
                        retry_after = getattr(e, 'retry_after', 1) + 0.5
                        self.log.warning(f"Rate limited during individual delete, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        retry_delay = min(retry_delay * 2, 5)  # Exponential backoff
                        # Retry this message
                        try:
                            await message.delete()
                            total_deleted += 1
                        except (discord.NotFound, discord.Forbidden):
                            continue
                    else:
                        raise
                
                # Reset backoff occasionally
                if total_deleted % 10 == 0:
                    retry_delay = max(base_delay, retry_delay * 0.75)
                    
        except discord.Forbidden:
            raise
            
        return total_deleted

    @commands.guild_only()
    @commands.hybrid_command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="User to ban", reason="Reason for the ban", delete_days="Number of days of messages to delete")
    async def ban(self, ctx: commands.Context, user: discord.User, *, reason: str = "No reason provided", delete_days: int = 0):
        """Ban a member from the server."""
        # Check permissions first
        if not await self._check_mod_permissions(ctx):
            await ctx.send("You don't have permission to use this command!", ephemeral=True)
            return

        # Convert to interaction if it's a slash command
        interaction = ctx.interaction if hasattr(ctx, 'interaction') else None
        
        if not await self._check_mod_permissions(interaction or ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        if not ctx.guild:
            return await ctx.send("This command can only be used in a server!", ephemeral=True)
            
        try:
            await ctx.guild.ban(user, reason=reason, delete_message_days=delete_days)
            
            # Log the action
            await self.bot.db.insert_mod_action(
                ctx.guild.id,
                user.id,
                "ban",
                reason,
                ctx.author.id
            )
            
            embed = discord.Embed(
                title="🔨 Member Banned",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=False)
            embed.add_field(name="Reason", value=reason, inline=False)
            
            await ctx.send(embed=embed)
            
        except discord.Forbidden as e:
            self.log.warning(f"Permission error in ban command: {e}")
            await ctx.send("I don't have permission to ban that user!", ephemeral=True)
        except Exception as e:
            self.log.error(f"Error in ban command: {e}", exc_info=True)
            await ctx.send("❌ An unexpected error occurred.", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="User to kick", reason="Reason for the kick")
    async def kick(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
        """Kick a member from the server."""
        
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        try:
            await user.kick(reason=reason)
            
            await self.bot.db.insert_mod_action(
                ctx.guild,
                user.id,
                "kick",
                reason,
                ctx.author.id
            )
            
            embed = discord.Embed(
                title="👢 Member Kicked",
                color=discord.Color.orange(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=False)
            embed.add_field(name="Reason", value=reason, inline=False)
            
            await ctx.send(embed=embed)
            
        except discord.Forbidden:
            await ctx.send("I don't have permission to kick that user!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="timeout", description="Timeout a member temporarily.")
    @app_commands.describe(
        user="User to timeout",
        duration="Duration in minutes (1-40320)", # 28 days max
        reason="Reason for the timeout"
    )
    async def timeout(self, ctx: commands.Context, user: discord.Member, duration: int, *, reason: str = "No reason provided"):
        """Timeout a member temporarily."""
        
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        # Add bounds checking for duration
        if duration < 1 or duration > 40320:  # 28 days in minutes
            return await ctx.send("Duration must be between 1 minute and 28 days!", ephemeral=True)
            
        try:
            until = datetime.utcnow() + timedelta(minutes=duration)
            await user.timeout(until, reason=reason)
            
            await self.bot.db.insert_mod_action(
                ctx.guild,
                user.id,
                "timeout",
                f"{reason} (Duration: {duration} minutes)",
                ctx.author.id
            )
            
            embed = discord.Embed(
                title="⏳ Member Timed Out",
                color=discord.Color.yellow(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            embed.add_field(name="Duration", value=f"{duration} minutes", inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=False)
            embed.add_field(name="Reason", value=reason, inline=False)
            
            await ctx.send(embed=embed)
            
        except discord.Forbidden:
            await ctx.send("I don't have permission to timeout that user!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="warn", description="Warn a member.")
    @app_commands.describe(user="User to warn", reason="Reason for the warning")
    async def warn(self, ctx: commands.Context, user: discord.Member, *, reason: str = "No reason provided"):
        """Warn a member."""
        
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        try:
            await self.bot.db.insert_mod_action(
                ctx.guild,
                user.id,
                "warn",
                reason,
                ctx.author.id
            )
            
            embed = discord.Embed(
                title="⚠️ Member Warned",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            embed.add_field(name="Moderator", value=ctx.author.mention, inline=False)
            embed.add_field(name="Reason", value=reason, inline=False)
            
            await ctx.send(embed=embed)
            
            try:
                # Try to DM the user
                warn_dm = discord.Embed(
                    title=f"⚠️ Warning from {ctx.guild}",
                    color=discord.Color.gold(),
                    description=f"You have been warned by {ctx.author}",
                    timestamp=datetime.utcnow()
                )
                warn_dm.add_field(name="Reason", value=reason)
                await user.send(embed=warn_dm)
            except:
                pass  # Ignore if we can't DM the user
                
        except Exception as e:
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="purge", description="Delete a number of messages.")
    @app_commands.describe(amount="Number of messages to delete (1-200)")
    async def purge(self, ctx: commands.Context, amount: int):
        """Delete a number of messages from the channel."""
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        if not isinstance(ctx.channel, discord.TextChannel):
            return await ctx.send("This command can only be used in text channels!", ephemeral=True)
            
        # Add bounds checking for amount
        if amount < 1 or amount > 200:
            return await ctx.send("Please provide a number between 1 and 200.", ephemeral=True)

        # Defer the response since this might take a while
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        
        try:
            # Delete command message first
            try:
                await ctx.message.delete()
            except (discord.NotFound, AttributeError):
                pass  # Message might be already deleted or not exist (slash command)
            
            deleted_count = await self._chunked_purge(ctx.channel, amount)
              # Log the action if we have a valid guild
            if ctx.guild:
                await self.bot.db.insert_mod_action(
                    ctx.guild.id,
                    ctx.author.id,
                    "purge",
                    f"Purged {deleted_count} messages in #{ctx.channel.name}",
                    ctx.author.id
                )
            
            response = f"✨ Successfully deleted {deleted_count} message{'s' if deleted_count != 1 else ''}."
            if deleted_count < amount:
                response += f"\nNote: Could not delete {amount - deleted_count} messages (they might be too old)."
            
            # Send response based on context
            if hasattr(ctx, 'interaction') and ctx.interaction:
                if hasattr(ctx.interaction, 'response') and ctx.interaction.response.is_done():
                    await ctx.interaction.followup.send(response, ephemeral=True)
                else:
                    await ctx.interaction.response.send_message(response, ephemeral=True)
            else:
                # For text command, send and then delete after 5 seconds
                msg = await ctx.send(response)
                await asyncio.sleep(5)
                try:
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                    
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages!", ephemeral=True)
        except Exception as e:
            self.log.error(f"Error in purge command: {e}", exc_info=True)
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="lockdown", description="Lock or unlock a channel.")
    @app_commands.describe(channel="Channel to lock/unlock", lock="True to lock, False to unlock")
    async def lockdown(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None, lock: bool = True):
        """Lock or unlock a channel."""
        
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(channel, discord.TextChannel):
            return await ctx.send("This command can only be used on text channels!", ephemeral=True)
            
        if not ctx.guild:
            return await ctx.send("This command can only be used in a server!", ephemeral=True)
            
        try:
            await channel.set_permissions(ctx.guild.default_role,
                                       send_messages=not lock,
                                       reason=f"Channel {'locked' if lock else 'unlocked'} by {ctx.author}")
            
            await self.bot.db.insert_mod_action(
                ctx.guild.id,
                ctx.author.id,
                "lockdown",
                f"{'Locked' if lock else 'Unlocked'} channel #{channel.name}",
                ctx.author.id
            )
            
            await ctx.send(f"🔒 Channel has been {'locked' if lock else 'unlocked'}.")
            
        except discord.Forbidden:
            await ctx.send("I don't have permission to modify channel permissions!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)
    
    @commands.guild_only()
    @commands.hybrid_command(name="slowmode", description="Set slowmode for a channel.")
    @app_commands.describe(seconds="Slowmode delay in seconds", channel="Channel to set slowmode for")
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: Optional[discord.TextChannel] = None):
        """Set slowmode for a channel."""
        
        if not await self._check_mod_permissions(ctx.interaction if hasattr(ctx, 'interaction') else ctx):
            return await ctx.send("You don't have permission to use this command!", ephemeral=True)
            
        channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(channel, discord.TextChannel):
            return await ctx.send("This command can only be used on text channels!", ephemeral=True)
            
        try:
            await channel.edit(slowmode_delay=seconds)
            
            await self.bot.db.insert_mod_action(
                ctx.guild,
                ctx.author.id,
                "slowmode",
                f"Set slowmode to {seconds}s in #{channel.name}",
                ctx.author.id
            )
            
            if seconds == 0:
                await ctx.send(f"🕒 Slowmode has been disabled in {channel.mention}")
            else:
                await ctx.send(f"🕒 Slowmode has been set to {seconds} seconds in {channel.mention}")
                
        except discord.Forbidden:
            await ctx.send("I don't have permission to modify channel settings!", ephemeral=True)
        except Exception as e:
            await ctx.send(f"An error occurred: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
