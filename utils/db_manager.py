import sqlite3
import logging
from datetime import datetime
import os
from pathlib import Path
import json
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_name: str):
        """Initialize database connection"""
        self.db_name = db_name
        self.db_path = os.path.join('db', f'{db_name}.db')
        os.makedirs('db', exist_ok=True)
        
        # Register datetime adapter
        sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
        sqlite3.register_converter('TIMESTAMP', lambda b: datetime.fromisoformat(b.decode()))
        
        self.setup_database()

    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection with proper settings"""
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        conn.row_factory = sqlite3.Row
        return conn

    def cursor(self):
        """Get a database cursor with automatic connection handling"""
        return self.get_connection().cursor()

    def setup_database(self) -> None:
        """Initialize database schema"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            
            # Enable foreign keys
            cur.execute("PRAGMA foreign_keys = ON")
            
            # Create tables
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS guilds (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    prefix TEXT DEFAULT '!',
                    locale TEXT DEFAULT 'en-US',
                    timezone TEXT DEFAULT 'UTC'
                );

                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    welcome_channel_id INTEGER,
                    goodbye_channel_id INTEGER,
                    log_channel_id INTEGER,
                    mute_role_id INTEGER,
                    autorole_enabled BOOLEAN DEFAULT 0,
                    autorole_id INTEGER,
                    level_channel_id INTEGER,
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER,
                    guild_id INTEGER,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 0,
                    messages INTEGER DEFAULT 0,
                    last_xp TIMESTAMP,
                    PRIMARY KEY (user_id, guild_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS command_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    user_id INTEGER,
                    command_name TEXT,
                    success BOOLEAN,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS guild_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    member_count INTEGER,
                    active_users INTEGER,
                    message_count INTEGER DEFAULT 0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS leveling_settings (
                    guild_id INTEGER PRIMARY KEY,
                    xp_cooldown INTEGER DEFAULT 60,
                    min_xp INTEGER DEFAULT 15,
                    max_xp INTEGER DEFAULT 25,
                    level_up_message TEXT DEFAULT 'Congratulations {user}, you reached level {level}!',
                    level_up_channel_id INTEGER,
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS level_roles (
                    guild_id INTEGER,
                    level INTEGER,
                    role_id INTEGER,
                    PRIMARY KEY (guild_id, level),
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER,
                    guild_id INTEGER,
                    notifications_enabled BOOLEAN DEFAULT 1,
                    private_levels BOOLEAN DEFAULT 0,
                    PRIMARY KEY (user_id, guild_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS xp_data (
                    user_id INTEGER,
                    guild_id INTEGER,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    messages INTEGER DEFAULT 0,
                    last_message TIMESTAMP,
                    PRIMARY KEY (user_id, guild_id),
                    FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id, guild_id) REFERENCES user_settings(user_id, guild_id) ON DELETE CASCADE
                );
            """)

            # Create indexes
            cur.executescript("""
                CREATE INDEX IF NOT EXISTS idx_user_stats_guild ON user_stats(guild_id);
                CREATE INDEX IF NOT EXISTS idx_command_logs_guild ON command_logs(guild_id);
                CREATE INDEX IF NOT EXISTS idx_metrics_guild_time ON guild_metrics(guild_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_xp_data_guild ON xp_data(guild_id);
                CREATE INDEX IF NOT EXISTS idx_xp_data_level ON xp_data(level);
                CREATE INDEX IF NOT EXISTS idx_level_roles_guild ON level_roles(guild_id);
            """)

    def ensure_guild_exists(self, guild_id: int, guild_name: str = None) -> None:
        """Ensure guild exists in database"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            
            # Check if guild exists
            cur.execute("SELECT 1 FROM guilds WHERE id = ?", (guild_id,))
            if not cur.fetchone():
                # Insert new guild
                cur.execute(
                    "INSERT INTO guilds (id, name) VALUES (?, ?)",
                    (guild_id, guild_name or str(guild_id))
                )
                # Create default settings
                cur.execute(
                    "INSERT INTO guild_settings (guild_id) VALUES (?)",
                    (guild_id,)
                )
                conn.commit()

    def get_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Get guild settings"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT g.*, gs.*
                FROM guilds g
                LEFT JOIN guild_settings gs ON g.id = gs.guild_id
                WHERE g.id = ?
            """, (guild_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_guild_settings(self, guild_id: int, **settings) -> bool:
        """Update guild settings"""
        valid_columns = {
            'prefix', 'locale', 'timezone', 'welcome_channel_id',
            'goodbye_channel_id', 'log_channel_id', 'mute_role_id',
            'autorole_enabled', 'autorole_id', 'level_channel_id'
        }
        
        # Filter invalid columns
        settings = {k: v for k, v in settings.items() if k in valid_columns}
        if not settings:
            return False

        with self.get_connection() as conn:
            cur = conn.cursor()
            for key, value in settings.items():
                table = 'guilds' if key in {'prefix', 'locale', 'timezone'} else 'guild_settings'
                cur.execute(
                    f"UPDATE {table} SET {key} = ? WHERE {'id' if table == 'guilds' else 'guild_id'} = ?",
                    (value, guild_id)
                )
            conn.commit()
            return True

    def batch_update_metrics(self, metrics: List[Dict[str, Any]]) -> None:
        """Batch insert guild metrics"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO guild_metrics 
                (guild_id, member_count, active_users, message_count, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (
                    m['guild_id'],
                    m['member_count'],
                    m['active_users'],
                    m.get('message_count', 0),
                    m.get('timestamp', datetime.now())
                )
                for m in metrics
            ])
            conn.commit()

    def log_command(self, guild_id: int, user_id: int, command_name: str,
                   success: bool, error: str = None) -> None:
        """Log command usage"""
        with self.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO command_logs 
                (guild_id, user_id, command_name, success, error)
                VALUES (?, ?, ?, ?, ?)
            """, (guild_id, user_id, command_name, success, error))
            conn.commit()

    def backup_database(self) -> Optional[str]:
        """Create a backup of the database"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_dir = os.path.join('db', 'backups')
            os.makedirs(backup_dir, exist_ok=True)
            
            backup_path = os.path.join(backup_dir, f'{self.db_name}_{timestamp}.db')
            
            with self.get_connection() as src, \
                 sqlite3.connect(backup_path) as dst:
                src.backup(dst)
            
            # Keep only last 5 backups
            backups = sorted(Path(backup_dir).glob(f'{self.db_name}_*.db'))
            for backup in backups[:-5]:
                backup.unlink()
                
            return backup_path
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return None