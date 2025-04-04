import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import random
import asyncio
import sys
import logging
from dotenv import load_dotenv
import colorama
from colorama import Fore, Style
import time
from datetime import datetime, timedelta
import sqlite3
from utils.db_manager import DatabaseManager

# Initialize colorama for Windows
colorama.init()

def check_dependencies():
    """Check and warn about missing optional dependencies"""
    try:
        import nacl
        return True
    except ImportError:
        logger.warning("PyNaCl is not installed. Voice features will be disabled.")
        logger.info("To install PyNaCl, run: pip install PyNaCl")
        return False

# Custom formatter for console output
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'WARNING': Fore.YELLOW,
        'ERROR': Fore.RED,
        'INFO': Fore.CYAN,
        'DEBUG': Fore.GREEN
    }

    def format(self, record):
        if record.msg is None:
            return ''
            
        msg_str = str(record.msg)
        
        # Special handling for discord.client and discord.gateway logs
        if record.name in ('discord.client', 'discord.gateway', 'discord.http'):
            # Convert these logs to our format
            clean_msg = msg_str
            for filter_msg in ('logging in using static token', 'has connected to Gateway'):
                if filter_msg in msg_str:
                    return ''  # Filter out these messages completely
            return f"{self.COLORS['INFO']}INFO{Style.RESET_ALL} | {clean_msg}"
                
        # Clean up bot status message
        if "Bot is in" in msg_str:
            guild_count = msg_str.split()[3]
            return f"{self.COLORS['INFO']}INFO{Style.RESET_ALL} | Active in {guild_count} guilds"
                
        if record.levelname in self.COLORS:
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{Style.RESET_ALL}"
            
        return f"{record.levelname} | {msg_str}"

# Create logs directory if it doesn't exist
def setup_logging_directory():
    """Ensure logging directory exists and is writable"""
    try:
        # For Replit compatibility - check if we're in Replit environment
        if 'REPL_ID' in os.environ:
            log_dir = os.path.join(os.getcwd(), 'logs')
        else:
            log_dir = 'logs'
            
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, 'bot.log')
    except Exception as e:
        print(f"Failed to setup logging directory: {e}")
        return 'bot.log'  # Fallback to current directory

# Create required directories
os.makedirs('logs', exist_ok=True)
os.makedirs('cogs', exist_ok=True)
os.makedirs('db', exist_ok=True)  # Add database directory

# Configure handlers
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColoredFormatter())

log_file = setup_logging_directory()
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# Clear any existing handlers from all loggers
logging.getLogger().handlers.clear()
logging.getLogger('discord').handlers.clear()
logging.getLogger('discord.http').handlers.clear()
logging.getLogger('discord.gateway').handlers.clear()

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

# Configure discord loggers with our formatting
for logger_name in ('discord', 'discord.client', 'discord.gateway', 'discord.http'):
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers = []  # Clear any existing handlers
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

# Configure our bot logger
logger = logging.getLogger('onWhisper')
logger.setLevel(logging.INFO)
logger.propagate = False
logger.handlers = []  # Clear any existing handlers
logger.addHandler(console_handler)
logger.addHandler(file_handler)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

def adapt_datetime(val: datetime) -> str:
    """Adapt datetime objects to ISO format strings for SQLite"""
    return val.isoformat()

def convert_datetime(val: bytes) -> datetime:
    """Convert ISO format strings from SQLite to datetime objects"""
    try:
        return datetime.fromisoformat(val.decode())
    except (ValueError, AttributeError):
        return None

# Register the adapter and converter at module level
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix='!', intents=intents)
        self.start_time = time.time()
        self.db = DatabaseManager('bot')
        self._metrics_task = None
        self._last_metrics = {}  # Will store datetime objects
        self._metric_buffer = []
        self._buffer_lock = asyncio.Lock()
        self._last_flush = datetime.now()
        self._rate_limit_retries = 0  # Track rate limit retries

    async def setup_hook(self):
        """Set up the bot's database and metrics collection"""
        await self.load_cogs()
        self._metrics_task = self.loop.create_task(self._collect_metrics())
        self._flush_task = self.loop.create_task(self._flush_metrics())
        logger.info("Bot setup completed")

    async def _collect_metrics(self):
        """Collect guild metrics every 5 minutes"""
        try:
            while not self.is_closed():
                current_time = datetime.now()
                
                for guild in self.guilds:
                    last_collection = self._last_metrics.get(guild.id, datetime.min)  # Use datetime.min as default
                    if (current_time - last_collection).total_seconds() >= 300:  # 5 minutes
                        active_users = len([
                            m for m in guild.members 
                            if str(m.status) == "online" and not m.bot
                        ])
                        
                        async with self._buffer_lock:
                            self._metric_buffer.append({
                                'guild_id': guild.id,
                                'member_count': guild.member_count,
                                'active_users': active_users,
                                'timestamp': current_time
                            })
                        self._last_metrics[guild.id] = current_time
                
                await asyncio.sleep(60)  # Check every minute
        except asyncio.CancelledError:
            if self._metric_buffer:
                await self._flush_metrics_buffer()
        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")

    async def _flush_metrics(self):
        """Periodically flush collected metrics to database"""
        try:
            while not self.is_closed():
                current_time = datetime.now()
                if (current_time - self._last_flush).total_seconds() >= 300 or len(self._metric_buffer) >= 100:
                    await self._flush_metrics_buffer()
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            if self._metric_buffer:
                await self._flush_metrics_buffer()
        except Exception as e:
            logger.error(f"Error in metrics flush: {e}")

    async def _flush_metrics_buffer(self):
        """Flush metrics buffer to database"""
        async with self._buffer_lock:
            if not self._metric_buffer:
                return
            
            try:
                # Process in chunks of 50 to avoid overwhelming the database
                chunk_size = 50
                for i in range(0, len(self._metric_buffer), chunk_size):
                    chunk = self._metric_buffer[i:i + chunk_size]
                    await asyncio.to_thread(self.db.batch_update_metrics, chunk)
                
                self._metric_buffer.clear()
                self._last_flush = datetime.now()
            except Exception as e:
                logger.error(f"Error flushing metrics: {e}")

    async def load_cogs(self):
        """Load all cogs from the cogs directory."""
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    logger.info(f'Loaded cog: {filename[:-3]}')
                except Exception as e:
                    logger.error(f'Failed to load cog {filename[:-3]}: {str(e)}')

    async def on_ready(self):
        """Called when the bot is ready and connected to Discord."""
        try:
            # Initialize settings for all guilds
            for guild in self.guilds:
                try:
                    self.db.ensure_guild_exists(guild.id)
                except sqlite3.IntegrityError:
                    # Skip if guild settings already exist
                    continue
                except Exception as e:
                    logger.error(f"Error initializing guild {guild.id}: {e}")

            # Count and sync slash commands
            command_count = 0
            for cmd in self.tree.walk_commands():
                command_count += 1
            
            await self.tree.sync()
            logger.info(f"✓ Successfully registered {command_count} slash commands")
            
            change_activity.start()
            logger.info(f'{self.user} is ready!')
            logger.info(f'Bot is in {len(self.guilds)} guilds')
        except Exception as e:
            logger.error(f'Error in on_ready: {str(e)}')

    async def on_guild_join(self, guild):
        """Called when the bot joins a new guild"""
        try:
            self.db.ensure_guild_exists(guild.id)
            logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        except Exception as e:
            logger.error(f"Error setting up new guild {guild.name}: {e}")

    async def on_command_error(self, ctx, error):
        """Global error handler with logging"""
        if isinstance(error, commands.CommandNotFound):
            return

        self.db.log_command(
            guild_id=ctx.guild.id if ctx.guild else None,
            user_id=ctx.author.id,
            command_name=ctx.command.name if ctx.command else "unknown",
            success=False,
            error=str(error)
        )
        
        logger.error(f'Error occurred: {str(error)}')
        await ctx.send(f'An error occurred: {str(error)}')

    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command):
        """Log successful slash command usage"""
        if interaction.guild:
            self.db.log_command(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                command_name=command.name,
                success=True
            )

    async def on_error(self, event_method: str, *args, **kwargs):
        """Global error handler for bot events"""
        try:
            error = sys.exc_info()[1]
            if isinstance(error, discord.errors.HTTPException) and error.status == 429:
                retry_after = error.response.headers.get('Retry-After', 5)
                self._rate_limit_retries += 1
                wait_time = float(retry_after) * (2 ** self._rate_limit_retries)  # Exponential backoff
                logger.warning(f"Rate limited. Waiting {wait_time:.2f} seconds before retry. Retry count: {self._rate_limit_retries}")
                await asyncio.sleep(wait_time)
                if event_method == "start":
                    await self.start(TOKEN)
            else:
                self._rate_limit_retries = 0  # Reset retry counter on non-rate-limit errors
                logger.error(f"Error in {event_method}: {str(error)}")
        except Exception as e:
            logger.error(f"Error in error handler: {str(e)}")

# List of activities for the bot to cycle through
ACTIVITIES = [
    discord.Game(name="with commands"),
    discord.Activity(type=discord.ActivityType.watching, name="over the server"),
    discord.Activity(type=discord.ActivityType.listening, name="to commands"),
    discord.Game(name="with Python"),
    discord.Activity(type=discord.ActivityType.competing, name="in tasks")
]

@tasks.loop(minutes=10)
async def change_activity():
    """Change the bot's activity randomly every 10 minutes."""
    await bot.wait_until_ready()
    await bot.change_presence(activity=random.choice(ACTIVITIES))

def run_bot():
    """Start the bot with rate limit handling"""
    retries = 0
    max_retries = 5
    base_delay = 5

    while retries < max_retries:
        try:
            asyncio.run(bot.run(TOKEN))
            break
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limit error
                retries += 1
                delay = base_delay * (2 ** retries)  # Exponential backoff
                logger.warning(f"Rate limited on startup. Attempt {retries}/{max_retries}. Waiting {delay} seconds...")
                time.sleep(delay)
            else:
                raise
        except KeyboardInterrupt:
            logger.info('Bot shutdown initiated')
            break
        except Exception as e:
            logger.error(f'Failed to start bot: {str(e)}')
            break

if __name__ == '__main__':
    check_dependencies()
    bot = Bot()
    run_bot()

