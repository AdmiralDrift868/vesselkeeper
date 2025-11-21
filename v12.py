#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vessel Keeper - Enterprise Marine Vessel Management System
Senior-Level Refactor: Professional architecture with enhanced error handling,
threading, resource management, and modern UI patterns.
"""
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, Pango
import sys
import os
import sqlite3
import logging
import json
import csv
import datetime
import threading
import re
import hashlib
import zipfile
import tempfile
import shutil
import weakref
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Callable, Iterator
from enum import Enum, auto
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, Future
from queue import Queue, Empty, Full
import time
from pathlib import Path

# =============================================================================
# CONSTANTS AND CONFIGURATION
# =============================================================================
CONFIG_FILE = "vessel_keeper_config.json"
DATABASE_FILE = "vessel_keeper.db"
REPORTS_DIR = "reports"
BACKUP_DIR = "backups"

# =============================================================================
# ENHANCED LOGGING SETUP
# =============================================================================
class StructuredLogger:
    """Enterprise-grade structured logging with context"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._setup_logging()
    
    def _setup_logging(self):
        """Configure comprehensive logging"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s | %(name)-20s | %(levelname)-8s | %(threadName)-15s | %(message)s',
            handlers=[
                logging.FileHandler('vessel_keeper.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
    
    def info(self, message: str, **context):
        self.logger.info(f"{message} | {self._format_context(context)}")
    
    def error(self, message: str, **context):
        self.logger.error(f"{message} | {self._format_context(context)}")
    
    def warning(self, message: str, **context):
        self.logger.warning(f"{message} | {self._format_context(context)}")
    
    def debug(self, message: str, **context):
        self.logger.debug(f"{message} | {self._format_context(context)}")
    
    def critical(self, message: str, **context):
        self.logger.critical(f"{message} | {self._format_context(context)}")
    
    def _format_context(self, context: Dict) -> str:
        return " | ".join(f"{k}={v}" for k, v in context.items())

logger = StructuredLogger()

# =============================================================================
# ENUMERATIONS AND DATA CLASSES
# =============================================================================
class TaskStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    OVERDUE = auto()

class Priority(Enum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()

@dataclass
class Vessel:
    id: Optional[int] = None
    name: str = ""
    vessel_type: str = ""
    engine_make: str = ""
    engine_model: str = ""
    current_location: str = ""
    created_at: str = ""

@dataclass
class MaintenanceTask:
    id: Optional[int] = None
    vessel_id: int = 0
    system: str = ""
    task_name: str = ""
    description: str = ""
    next_due: str = ""
    status: str = "pending"
    priority: str = "medium"
    created_at: str = ""

# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================
class DatabaseError(Exception):
    """Base exception for database-related errors"""
    pass

class ValidationError(Exception):
    """Raised when data validation fails"""
    pass

class ResourceError(Exception):
    """Raised when resource allocation fails"""
    pass

class MigrationError(DatabaseError):
    """Raised when database migrations fail"""
    pass

# =============================================================================
# CONFIGURATION MANAGEMENT
# =============================================================================
class AppConfig:
    """Type-safe application configuration with validation"""
    
    def __init__(self):
        self.window_x: int = 100
        self.window_y: int = 100
        self.window_width: int = 1200
        self.window_height: int = 800
        self.due_soon_days: int = 7
        self.due_soon_hours: int = 10
        self.max_upload_size: int = 10 * 1024 * 1024  # 10MB
        self.backup_retention_days: int = 30
        self.theme: str = "dark"
        self.auto_backup: bool = True

class ConfigManager:
    """Enhanced configuration management with atomic operations"""
    
    def __init__(self, config_file: str = CONFIG_FILE):
        self.config_file = config_file
        self._config = AppConfig()
        self._lock = threading.RLock()
        self._load_config()
    
    def _load_config(self):
        """Safely load configuration with validation"""
        if not os.path.exists(self.config_file):
            logger.info("No config file found, using defaults")
            return
        
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                user_config = json.load(f)
            
            with self._lock:
                for key, value in user_config.items():
                    if hasattr(self._config, key):
                        setattr(self._config, key, value)
            
            logger.info("Configuration loaded successfully")
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Config load failed, using defaults: {e}")
    
    def get(self, key: str, default=None):
        """Get configuration value with thread safety"""
        with self._lock:
            return getattr(self._config, key, default)
    
    def set(self, key: str, value):
        """Set configuration value with validation"""
        with self._lock:
            if hasattr(self._config, key):
                setattr(self._config, key, value)
    
    def save(self):
        """Save configuration with atomic write"""
        try:
            temp_file = f"{self.config_file}.tmp"
            
            with open(temp_file, 'w', encoding='utf-8') as f:
                config_dict = {key: getattr(self._config, key) 
                             for key in dir(self._config) 
                             if not key.startswith('_')}
                json.dump(config_dict, f, indent=2, ensure_ascii=False)
            
            os.replace(temp_file, self.config_file)
            logger.info("Configuration saved successfully")
        except IOError as e:
            logger.error(f"Failed to save configuration: {e}")
            raise

# =============================================================================
# DATABASE CONNECTION POOL
# =============================================================================
class DatabaseConnectionPool:
    """Thread-safe database connection pool with health monitoring"""
    
    def __init__(self, db_path: str, max_connections: int = 5, timeout: float = 30.0):
        self.db_path = db_path
        self.max_connections = max_connections
        self.timeout = timeout
        self._connections: Queue[sqlite3.Connection] = Queue(max_connections)
        self._in_use: set = set()
        self._lock = threading.RLock()
        self._initialize_pool()
    
    def _initialize_pool(self):
        """Initialize connection pool with optimized settings"""
        for _ in range(self.max_connections):
            conn = self._create_connection()
            self._connections.put(conn)
    
    def _create_connection(self) -> sqlite3.Connection:
        """Create optimized database connection"""
        try:
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.timeout,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            
            # Performance optimizations
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            conn.execute("PRAGMA temp_store = MEMORY")
            
            return conn
        except sqlite3.Error as e:
            logger.error(f"Failed to create database connection: {e}")
            raise DatabaseError(f"Database connection failed: {e}") from e
    
    def _health_check(self, conn: sqlite3.Connection) -> bool:
        """Verify connection health"""
        try:
            conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False
    
    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Thread-safe connection context manager"""
        conn = None
        try:
            conn = self._acquire_connection()
            yield conn
            conn.commit()
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            logger.error(f"Database operation failed: {e}")
            raise DatabaseError(f"Database error: {e}") from e
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Unexpected error in database operation: {e}")
            raise
        finally:
            if conn:
                self._release_connection(conn)
    
    def _acquire_connection(self) -> sqlite3.Connection:
        """Acquire connection with timeout and health check"""
        deadline = time.time() + self.timeout
        
        while time.time() < deadline:
            try:
                conn = self._connections.get(timeout=1)
                if self._health_check(conn):
                    with self._lock:
                        self._in_use.add(id(conn))
                    return conn
                else:
                    # Replace unhealthy connection
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                    new_conn = self._create_connection()
                    with self._lock:
                        self._in_use.add(id(new_conn))
                    return new_conn
            except Empty:
                continue
        
        raise DatabaseError("Timeout acquiring database connection")
    
    def _release_connection(self, conn: sqlite3.Connection):
        """Release connection back to pool"""
        try:
            with self._lock:
                conn_id = id(conn)
                if conn_id in self._in_use:
                    self._in_use.remove(conn_id)
            
            if self._health_check(conn):
                self._connections.put(conn, timeout=1)
            else:
                conn.close()
                new_conn = self._create_connection()
                self._connections.put(new_conn, timeout=1)
        except Full:
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"Error releasing connection: {e}")
            conn.close()
    
    def close_all(self):
        """Close all connections gracefully"""
        with self._lock:
            while not self._connections.empty():
                try:
                    conn = self._connections.get_nowait()
                    conn.close()
                except Empty:
                    break
            self._in_use.clear()

# =============================================================================
# ENTERPRISE DATABASE LAYER
# =============================================================================
class DatabaseMetrics:
    """Database performance and health monitoring"""
    
    def __init__(self):
        self._lock = threading.RLock()
        self._query_count = 0
        self._error_count = 0
        self._total_query_time = 0.0
        self._slow_query_threshold = 1.0
    
    def record_success(self, duration: float):
        with self._lock:
            self._query_count += 1
            self._total_query_time += duration
            if duration > self._slow_query_threshold:
                logger.warning(f"Slow query detected: {duration:.3f}s")
    
    def record_error(self, duration: float):
        with self._lock:
            self._error_count += 1
            self._total_query_time += duration
    
    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            avg_time = (self._total_query_time / self._query_count) if self._query_count > 0 else 0
            error_rate = (self._error_count / self._query_count * 100) if self._query_count > 0 else 0
            
            return {
                "total_queries": self._query_count,
                "error_count": self._error_count,
                "error_rate_percent": error_rate,
                "average_query_time_seconds": avg_time,
                "total_query_time_seconds": self._total_query_time
            }

class ThreadSafeDatabase:
    """Enterprise-grade database layer with comprehensive error handling"""
    
    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        self.connection_pool = DatabaseConnectionPool(db_path)
        self._query_lock = threading.RLock()
        self._metrics = DatabaseMetrics()
        self._ensure_database_integrity()
    
    def _ensure_database_integrity(self):
        """Comprehensive database integrity verification and migration"""
        logger.info("Starting database integrity verification")
        
        if not self._verify_database_file():
            self._initialize_new_database()
            return
        
        integrity_status = self._perform_integrity_check()
        if not integrity_status["healthy"]:
            self._handle_database_corruption(integrity_status)
        
        self._perform_safe_migrations()
        logger.info("Database integrity verified successfully")
    
    def _verify_database_file(self) -> bool:
        """Verify database file structure and accessibility"""
        try:
            if not os.path.exists(self.db_path):
                logger.warning(f"Database file does not exist: {self.db_path}")
                return False
            
            stat_info = os.stat(self.db_path)
            if stat_info.st_size == 0:
                logger.error("Database file is empty")
                return False
            
            with open(self.db_path, 'rb') as f:
                header = f.read(16)
                if not header.startswith(b'SQLite format 3'):
                    logger.error("Invalid SQLite database format")
                    return False
            
            return True
            
        except (OSError, IOError) as e:
            logger.error(f"Database file access error: {e}")
            return False
    
    def _perform_integrity_check(self) -> Dict[str, Any]:
        """Comprehensive integrity check with detailed reporting"""
        integrity_result = {
            "healthy": False,
            "errors": [],
            "warnings": [],
            "repair_attempted": False,
            "backup_created": False
        }
        
        try:
            with self.connection_pool.get_connection() as conn:
                # Quick integrity check
                result = conn.execute("PRAGMA quick_check").fetchone()
                if result[0] != "ok":
                    integrity_result["errors"].append(f"Quick check failed: {result[0]}")
                    
                    # Full integrity check for details
                    full_result = conn.execute("PRAGMA integrity_check").fetchall()
                    integrity_result["errors"].extend([row[0] for row in full_result if row[0] != "ok"])
                    
                    logger.error(f"Database integrity check failed: {integrity_result['errors']}")
                    return integrity_result
                
                # Foreign key verification
                fk_result = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_result:
                    fk_errors = [f"Foreign key violation: table={row[0]}, rowid={row[1]}" 
                               for row in fk_result]
                    integrity_result["errors"].extend(fk_errors)
                
                integrity_result["healthy"] = len(integrity_result["errors"]) == 0
                
        except sqlite3.Error as e:
            integrity_result["errors"].append(f"Integrity check execution failed: {e}")
        
        return integrity_result
    
    def _handle_database_corruption(self, integrity_status: Dict[str, Any]):
        """Handle database corruption with recovery strategies"""
        logger.critical("Handling database corruption")
        
        # Create emergency backup
        self._create_emergency_backup("corruption")
        
        # Attempt repair strategies
        if not self._attempt_automatic_repair(integrity_status):
            if not self._restore_from_backup():
                self._initialize_new_database()
    
    def _attempt_automatic_repair(self, integrity_status: Dict[str, Any]) -> bool:
        """Attempt automatic database repair"""
        logger.info("Attempting automatic database repair")
        
        try:
            with self.connection_pool.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                
                # Re-verify integrity
                result = conn.execute("PRAGMA quick_check").fetchone()
                if result[0] == "ok":
                    logger.info("Automatic repair successful")
                    return True
                    
        except sqlite3.Error as e:
            logger.error(f"Automatic repair failed: {e}")
        
        return False
    
    def _perform_safe_migrations(self):
        """Safe database migrations with rollback capability"""
        logger.info("Starting safe database migrations")
        
        migration_backup = self._create_emergency_backup("pre_migration")
        if not migration_backup:
            logger.error("Cannot proceed with migrations: backup creation failed")
            return
        
        try:
            with self.connection_pool.get_connection() as conn:
                current_version = self._get_schema_version(conn)
                migrations = self._get_migrations()
                
                migrations_to_apply = [(v, m) for v, m in migrations.items() if v > current_version]
                
                if not migrations_to_apply:
                    logger.info("No migrations required")
                    return
                
                conn.execute("BEGIN IMMEDIATE")
                
                for target_version, migration_sql in sorted(migrations_to_apply):
                    logger.info(f"Applying migration to version {target_version}")
                    conn.executescript(migration_sql)
                    conn.execute(f"PRAGMA user_version = {target_version}")
                
                conn.commit()
                logger.info("All migrations completed successfully")
                
        except Exception as e:
            logger.error(f"Migration process failed: {e}")
            self._restore_from_specific_backup(migration_backup)
    
    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """Get current schema version"""
        try:
            result = conn.execute("PRAGMA user_version").fetchone()
            return result[0] if result else 0
        except sqlite3.Error:
            return 0
    
    def _get_migrations(self) -> Dict[int, str]:
        """Define database schema migrations"""
        return {
            1: """
            -- Initial schema
            CREATE TABLE IF NOT EXISTS vessels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                vessel_type TEXT,
                engine_make TEXT,
                engine_model TEXT,
                current_location TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                part_number TEXT,
                category TEXT,
                current_stock INTEGER DEFAULT 0,
                minimum_stock INTEGER DEFAULT 0,
                unit_cost REAL,
                supplier_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
            );
            
            CREATE TABLE IF NOT EXISTS maintenance_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_id INTEGER NOT NULL,
                system TEXT,
                task_name TEXT NOT NULL,
                description TEXT,
                next_due TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
            );
            
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                contact TEXT,
                phone TEXT,
                email TEXT,
                preferred INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS trip_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_id INTEGER NOT NULL,
                departure TEXT,
                arrival TEXT,
                destination TEXT,
                distance REAL,
                fuel_used REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
            );
            
            CREATE TABLE IF NOT EXISTS stock_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                quantity INTEGER,
                transaction_type TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (item_id) REFERENCES inventory_items(id) ON DELETE CASCADE
            );
            """,
            2: """
            -- Performance indexes and enhancements
            CREATE INDEX IF NOT EXISTS idx_maintenance_tasks_vessel_id ON maintenance_tasks(vessel_id);
            CREATE INDEX IF NOT EXISTS idx_maintenance_tasks_status ON maintenance_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_inventory_items_supplier_id ON inventory_items(supplier_id);
            CREATE INDEX IF NOT EXISTS idx_trip_logs_vessel_id ON trip_logs(vessel_id);
            CREATE INDEX IF NOT EXISTS idx_stock_transactions_item_id ON stock_transactions(item_id);
            
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                metric_name TEXT NOT NULL,
                value REAL,
                unit TEXT,
                date_recorded TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_performance_metrics_vessel_id ON performance_metrics(vessel_id);
            """
        }
    
    def _create_emergency_backup(self, reason: str) -> Optional[str]:
        """Create emergency backup"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = Path("backups/emergency")
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            backup_path = backup_dir / f"emergency_{reason}_{timestamp}.db"
            
            with self.connection_pool.get_connection() as conn:
                conn.execute(f"VACUUM INTO '{backup_path}'")
            
            logger.info(f"Emergency backup created: {backup_path}")
            return str(backup_path)
            
        except Exception as e:
            logger.error(f"Emergency backup creation failed: {e}")
            return None
    
    def _restore_from_backup(self) -> bool:
        """Restore from latest backup"""
        try:
            backup_dir = Path("backups")
            if not backup_dir.exists():
                return False
            
            backups = list(backup_dir.glob("*.db"))
            if not backups:
                return False
            
            latest_backup = max(backups, key=lambda p: p.stat().st_mtime)
            
            # Verify backup integrity
            with sqlite3.connect(str(latest_backup)) as test_conn:
                result = test_conn.execute("PRAGMA quick_check").fetchone()
                if result[0] != "ok":
                    return False
            
            self.connection_pool.close_all()
            shutil.copy2(str(latest_backup), self.db_path)
            self.connection_pool = DatabaseConnectionPool(self.db_path)
            
            logger.info(f"Successfully restored from backup: {latest_backup}")
            return True
            
        except Exception as e:
            logger.error(f"Backup restoration failed: {e}")
            return False
    
    def _restore_from_specific_backup(self, backup_path: str) -> bool:
        """Restore from specific backup file"""
        try:
            self.connection_pool.close_all()
            shutil.copy2(backup_path, self.db_path)
            self.connection_pool = DatabaseConnectionPool(self.db_path)
            return True
        except Exception as e:
            logger.error(f"Specific backup restoration failed: {e}")
            return False
    
    def _initialize_new_database(self):
        """Initialize new database with schema"""
        logger.warning("Initializing new database")
        
        try:
            self.connection_pool.close_all()
            
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            
            self.connection_pool = DatabaseConnectionPool(self.db_path)
            
            with self.connection_pool.get_connection() as conn:
                initial_migration = self._get_migrations()[1]  # Get initial schema
                conn.executescript(initial_migration)
                conn.execute("PRAGMA user_version = 2")
            
            logger.info("New database initialized successfully")
            
        except Exception as e:
            logger.error(f"New database initialization failed: {e}")
            raise
    
    @contextmanager
    def get_cursor(self) -> Iterator[sqlite3.Cursor]:
        """Thread-safe cursor context manager"""
        start_time = time.time()
        with self._query_lock, self.connection_pool.get_connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor
                self._metrics.record_success(time.time() - start_time)
            except sqlite3.Error as e:
                self._metrics.record_error(time.time() - start_time)
                logger.error(f"Database operation failed: {e}")
                raise
            except Exception as e:
                self._metrics.record_error(time.time() - start_time)
                logger.error(f"Unexpected error in database operation: {e}")
                raise
    
    def execute_query(self, query: str, params: Tuple = ()) -> List[Dict]:
        """Execute query and return results as dictionaries"""
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def execute_update(self, query: str, params: Tuple = ()) -> int:
        """Execute update query and return rowcount"""
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            return cursor.rowcount
    
    def get_vessels(self) -> List[Dict]:
        """Get all vessels"""
        return self.execute_query("SELECT * FROM vessels ORDER BY name")
    
    def get_maintenance_tasks(self) -> List[Dict]:
        """Get all maintenance tasks"""
        return self.execute_query("""
            SELECT mt.*, v.name as vessel_name 
            FROM maintenance_tasks mt 
            LEFT JOIN vessels v ON mt.vessel_id = v.id 
            ORDER BY mt.next_due
        """)
    
    def get_inventory_items(self) -> List[Dict]:
        """Get all inventory items"""
        return self.execute_query("SELECT * FROM inventory_items ORDER BY name")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get database performance metrics"""
        return self._metrics.get_summary()
    
    def create_backup(self, backup_path: str) -> bool:
        """Create verified database backup"""
        try:
            with self.connection_pool.get_connection() as conn:
                conn.execute(f"VACUUM INTO '{backup_path}'")
            
            # Verify backup
            with sqlite3.connect(backup_path) as backup_conn:
                result = backup_conn.execute("PRAGMA integrity_check").fetchone()
                if result[0] == "ok":
                    logger.info(f"Database backup created: {backup_path}")
                    return True
                else:
                    os.remove(backup_path)
                    return False
        except sqlite3.Error as e:
            logger.error(f"Backup creation failed: {e}")
            return False
    
    def close(self):
        """Close database connections"""
        self.connection_pool.close_all()

# =============================================================================
# UI STATE MANAGEMENT
# =============================================================================
class UIStateManager:
    """Thread-safe UI state management with action queuing"""
    
    def __init__(self):
        self._ui_actions: Queue[Tuple[Callable, tuple, dict]] = Queue()
        self._is_processing = False
        self._action_lock = threading.RLock()
    
    def queue_ui_action(self, action: Callable, *args, **kwargs):
        """Queue UI action for main thread execution"""
        with self._action_lock:
            self._ui_actions.put((action, args, kwargs))
            if not self._is_processing:
                self._is_processing = True
                GLib.idle_add(self._process_queued_actions)
    
    def _process_queued_actions(self) -> bool:
        """Process queued UI actions (main thread only)"""
        processed = False
        try:
            while True:
                action, args, kwargs = self._ui_actions.get_nowait()
                try:
                    action(*args, **kwargs)
                    processed = True
                except Exception as e:
                    logger.error(f"UI action failed: {e}")
        except Empty:
            pass
        
        with self._action_lock:
            if self._ui_actions.empty():
                self._is_processing = False
                return False
            else:
                return True
    
    def batch_update(self, store: Gtk.ListStore, updates: List[Tuple]):
        """Batch update ListStore to minimize UI refresh"""
        def _apply_batch():
            try:
                store.freeze_notify()
                for update in updates:
                    if update[0] == 'clear':
                        store.clear()
                    elif update[0] == 'append':
                        store.append(update[1])
                    elif update[0] == 'remove':
                        store.remove(update[1])
            finally:
                store.thaw_notify()
        
        self.queue_ui_action(_apply_batch)

# =============================================================================
# DATA LOADER WITH CACHING
# =============================================================================
class LazyDataLoader:
    """Efficient data loading with caching and background processing"""
    
    def __init__(self, db: ThreadSafeDatabase):
        self.db = db
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._load_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="DataLoader")
    
    def get_vessels_summary(self, force_refresh: bool = False) -> List[Dict]:
        cache_key = 'vessels_summary'
        return self._get_cached_data(cache_key, self._load_vessels_summary, force_refresh)
    
    def get_inventory_summary(self, force_refresh: bool = False) -> List[Dict]:
        cache_key = 'inventory_summary'
        return self._get_cached_data(cache_key, self._load_inventory_summary, force_refresh)
    
    def get_maintenance_tasks_summary(self, force_refresh: bool = False) -> List[Dict]:
        cache_key = 'maintenance_tasks_summary'
        return self._get_cached_data(cache_key, self._load_maintenance_tasks_summary, force_refresh)
    
    def _get_cached_data(self, cache_key: str, loader: Callable, force_refresh: bool) -> Any:
        current_time = time.time()
        
        with self._load_lock:
            if not force_refresh and cache_key in self._cache:
                timestamp, data = self._cache[cache_key]
                if current_time - timestamp < self._cache_ttl:
                    return data
            
            try:
                data = loader()
                self._cache[cache_key] = (current_time, data)
                return data
            except Exception as e:
                logger.error(f"Failed to load data for {cache_key}: {e}")
                if cache_key in self._cache:
                    return self._cache[cache_key][1]
                raise
    
    def _load_vessels_summary(self) -> List[Dict]:
        return self.db.get_vessels()
    
    def _load_inventory_summary(self) -> List[Dict]:
        return self.db.get_inventory_items()
    
    def _load_maintenance_tasks_summary(self) -> List[Dict]:
        return self.db.get_maintenance_tasks()
    
    def invalidate_cache(self, key: str = None):
        with self._load_lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()
    
    def shutdown(self):
        self._executor.shutdown(wait=True)

# =============================================================================
# REFRESH MANAGEMENT
# =============================================================================
class RefreshManager:
    """Manage view refreshes with deduplication and rate limiting"""
    
    def __init__(self, app):
        self.app = app
        self._refresh_lock = threading.RLock()
        self._is_refreshing = False
        self._pending_refresh = False
        self._last_refresh_time = 0
        self._min_refresh_interval = 1.0
    
    def schedule_refresh(self):
        current_time = time.time()
        
        with self._refresh_lock:
            if self._is_refreshing:
                self._pending_refresh = True
                return
            
            if current_time - self._last_refresh_time < self._min_refresh_interval:
                self._pending_refresh = True
                return
            
            self._is_refreshing = True
        
        def _refresh_task():
            try:
                self._perform_refresh()
            except Exception as e:
                logger.error(f"Background refresh failed: {e}")
            finally:
                with self._refresh_lock:
                    self._is_refreshing = False
                    self._last_refresh_time = time.time()
                    
                    if self._pending_refresh:
                        self._pending_refresh = False
                        GLib.timeout_add(100, self.schedule_refresh)
        
        threading.Thread(target=_refresh_task, name="RefreshWorker", daemon=True).start()
    
    def _perform_refresh(self):
        try:
            vessels_data = self.app.data_loader.get_vessels_summary(force_refresh=True)
            inventory_data = self.app.data_loader.get_inventory_summary(force_refresh=True)
            tasks_data = self.app.data_loader.get_maintenance_tasks_summary(force_refresh=True)
            
            self.app.ui_state_manager.queue_ui_action(
                self.app._update_all_views,
                vessels_data, inventory_data, tasks_data
            )
            
        except Exception as e:
            logger.error(f"Refresh data loading failed: {e}")
            self.app.ui_state_manager.queue_ui_action(
                self.app._update_status,
                f"Refresh failed: {e}"
            )

# =============================================================================
# MAIN APPLICATION
# =============================================================================
class VesselKeeperApp(Gtk.Window):
    """Enterprise-grade main application"""
    
    def __init__(self):
        super().__init__(title="Vessel Keeper - Professional Edition")
        self.set_default_size(1200, 800)
        
        # Initialize core components
        self.config_manager = ConfigManager()
        self.db = ThreadSafeDatabase()
        self.ui_state_manager = UIStateManager()
        self.data_loader = LazyDataLoader(self.db)
        self._refresh_manager = RefreshManager(self)
        
        # Application state
        self._is_shutting_down = False
        
        self._setup_ui()
        self._apply_css()
        self._restore_window_state()
        self._start_background_tasks()
        
        logger.info("Vessel Keeper application initialized successfully")
    
    def _setup_ui(self):
        """Setup professional UI with error handling"""
        try:
            main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.add(main_vbox)

            # Enhanced Header Bar
            header_bar = Gtk.HeaderBar()
            header_bar.set_show_close_button(True)
            header_bar.set_title("Vessel Keeper")
            header_bar.set_subtitle("Professional Marine Management System")
            
            # Menu button
            menu_button = Gtk.MenuButton()
            menu_icon = Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
            menu_button.set_image(menu_icon)
            
            # Application menu
            menu = Gio.Menu()
            
            # File section
            file_section = Gio.Menu()
            file_section.append("Backup Database", "app.backup")
            file_section.append("Preferences", "app.preferences")
            menu.append_section(None, file_section)
            
            # Help section
            help_section = Gio.Menu()
            help_section.append("About", "app.about")
            help_section.append("Quit", "app.quit")
            menu.append_section(None, help_section)
            
            menu_button.set_menu_model(menu)
            header_bar.pack_end(menu_button)
            
            self.set_titlebar(header_bar)

            # Main content
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            main_vbox.pack_start(content_box, True, True, 0)

            # Notebook with tabs
            self.notebook = Gtk.Notebook()
            self.notebook.set_scrollable(True)
            content_box.pack_end(self.notebook, True, True, 0)

            # Create tabs
            self.notebook.append_page(self._create_dashboard_tab(), Gtk.Label(label="Dashboard"))
            self.notebook.append_page(self._create_vessels_tab(), Gtk.Label(label="Vessels"))
            self.notebook.append_page(self._create_inventory_tab(), Gtk.Label(label="Inventory"))
            self.notebook.append_page(self._create_maintenance_tab(), Gtk.Label(label="Maintenance"))

            # Status bar
            status_bar = Gtk.Statusbar()
            self.status_context_id = status_bar.get_context_id("main")
            main_vbox.pack_end(status_bar, False, False, 0)
            self._update_status("Application ready")
            
        except Exception as e:
            logger.error(f"UI setup failed: {e}")
            raise ResourceError(f"Failed to initialize UI: {e}") from e
    
    def _create_dashboard_tab(self) -> Gtk.Widget:
        """Create dashboard tab"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(10)
        
        label = Gtk.Label(label="Dashboard - Summary and Analytics")
        label.get_style_context().add_class("professional-header")
        box.pack_start(label, False, False, 0)
        
        # Add some sample content
        content_label = Gtk.Label(label="Vessel overview and key metrics will appear here")
        box.pack_start(content_label, False, False, 0)
        
        refresh_btn = Gtk.Button(label="Refresh Data")
        refresh_btn.connect("clicked", lambda x: self.refresh_views())
        box.pack_start(refresh_btn, False, False, 0)
        
        return box
    
    def _create_vessels_tab(self) -> Gtk.Widget:
        """Create vessels management tab"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(10)
        
        label = Gtk.Label(label="Vessel Management")
        label.get_style_context().add_class("professional-header")
        box.pack_start(label, False, False, 0)
        
        # Vessels list
        vessels_list = Gtk.ListStore(str, str, str, str)
        treeview = Gtk.TreeView(model=vessels_list)
        
        # Add columns
        for i, column_name in enumerate(["Name", "Type", "Engine", "Location"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(column_name, renderer, text=i)
            treeview.append_column(column)
        
        scrollable = Gtk.ScrolledWindow()
        scrollable.set_vexpand(True)
        scrollable.add(treeview)
        box.pack_start(scrollable, True, True, 0)
        
        # Store reference for updates
        self.vessels_list = vessels_list
        
        return box
    
    def _create_inventory_tab(self) -> Gtk.Widget:
        """Create inventory management tab"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(10)
        
        label = Gtk.Label(label="Inventory Management")
        label.get_style_context().add_class("professional-header")
        box.pack_start(label, False, False, 0)
        
        # Inventory list
        inventory_list = Gtk.ListStore(str, str, str, int, int, float)
        treeview = Gtk.TreeView(model=inventory_list)
        
        # Add columns
        columns = ["Name", "Part Number", "Category", "Stock", "Min Stock", "Unit Cost"]
        for i, column_name in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(column_name, renderer, text=i)
            treeview.append_column(column)
        
        scrollable = Gtk.ScrolledWindow()
        scrollable.set_vexpand(True)
        scrollable.add(treeview)
        box.pack_start(scrollable, True, True, 0)
        
        # Store reference for updates
        self.inventory_list = inventory_list
        
        return box
    
    def _create_maintenance_tab(self) -> Gtk.Widget:
        """Create maintenance management tab"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(10)
        
        label = Gtk.Label(label="Maintenance Management")
        label.get_style_context().add_class("professional-header")
        box.pack_start(label, False, False, 0)
        
        # Maintenance tasks list
        tasks_list = Gtk.ListStore(str, str, str, str, str, str)
        treeview = Gtk.TreeView(model=tasks_list)
        
        # Add columns
        columns = ["Vessel", "System", "Task", "Due Date", "Status", "Priority"]
        for i, column_name in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(column_name, renderer, text=i)
            treeview.append_column(column)
        
        scrollable = Gtk.ScrolledWindow()
        scrollable.set_vexpand(True)
        scrollable.add(treeview)
        box.pack_start(scrollable, True, True, 0)
        
        # Store reference for updates
        self.tasks_list = tasks_list
        
        return box
    
    def _apply_css(self):
        """Apply professional CSS styling"""
        css_data = """
        .professional-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-weight: bold;
            padding: 10px;
            border-radius: 5px;
            font-size: 16px;
        }
        .status-overdue { 
            color: #dc3545; 
            font-weight: bold; 
        }
        .status-due-soon { 
            color: #fd7e14; 
            font-weight: bold;
        }
        .status-on-schedule { 
            color: #28a745; 
        }
        .validation-error {
            border-color: #dc3545;
            background-color: #f8d7da;
        }
        """
        style_provider = Gtk.CssProvider()
        try:
            style_provider.load_from_data(css_data.encode())
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                style_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as e:
            logger.warning(f"CSS loading failed: {e}")
    
    def _restore_window_state(self):
        """Restore window position and size"""
        try:
            x = self.config_manager.get("window_x", 100)
            y = self.config_manager.get("window_y", 100)
            width = self.config_manager.get("window_width", 1200)
            height = self.config_manager.get("window_height", 800)
            
            self.move(x, y)
            self.resize(width, height)
        except Exception as e:
            logger.warning(f"Failed to restore window state: {e}")
    
    def _start_background_tasks(self):
        """Start background maintenance tasks"""
        def backup_scheduler():
            while not self._is_shutting_down:
                try:
                    now = datetime.datetime.now()
                    if now.hour == 2 and now.minute == 0:
                        self._perform_automated_backup()
                    time.sleep(60)
                except Exception as e:
                    logger.error(f"Backup scheduler error: {e}")
                    time.sleep(300)
        
        backup_thread = threading.Thread(target=backup_scheduler, name="BackupScheduler", daemon=True)
        backup_thread.start()
        
        def cache_cleanup():
            while not self._is_shutting_down:
                try:
                    time.sleep(3600)
                    self.data_loader.invalidate_cache()
                    logger.debug("Cache cleaned up")
                except Exception as e:
                    logger.error(f"Cache cleanup error: {e}")
        
        cache_thread = threading.Thread(target=cache_cleanup, name="CacheCleanup", daemon=True)
        cache_thread.start()
    
    def _perform_automated_backup(self):
        """Perform automated database backup"""
        try:
            backup_dir = Path("backups")
            backup_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = backup_dir / f"vessel_keeper_backup_{timestamp}.db"
            
            if self.db.create_backup(str(backup_file)):
                logger.info(f"Automated backup created: {backup_file}")
                self._cleanup_old_backups(backup_dir)
            else:
                logger.error("Automated backup failed")
        except Exception as e:
            logger.error(f"Automated backup process failed: {e}")
    
    def _cleanup_old_backups(self, backup_dir: Path):
        """Clean up old backups"""
        retention_days = self.config_manager.get("backup_retention_days", 30)
        cutoff_time = time.time() - (retention_days * 24 * 3600)
        
        for backup_file in backup_dir.glob("vessel_keeper_backup_*.db"):
            if backup_file.stat().st_mtime < cutoff_time:
                try:
                    backup_file.unlink()
                    logger.info(f"Deleted old backup: {backup_file}")
                except OSError as e:
                    logger.warning(f"Failed to delete old backup {backup_file}: {e}")
    
    def refresh_views(self):
        """Refresh all views"""
        self._refresh_manager.schedule_refresh()
    
    def _update_all_views(self, vessels_data, inventory_data, tasks_data):
        """Update all UI views with fresh data"""
        # Update vessels list
        if hasattr(self, 'vessels_list'):
            self.vessels_list.clear()
            for vessel in vessels_data:
                self.vessels_list.append([
                    vessel.get('name', ''),
                    vessel.get('vessel_type', ''),
                    f"{vessel.get('engine_make', '')} {vessel.get('engine_model', '')}",
                    vessel.get('current_location', '')
                ])
        
        # Update inventory list
        if hasattr(self, 'inventory_list'):
            self.inventory_list.clear()
            for item in inventory_data:
                self.inventory_list.append([
                    item.get('name', ''),
                    item.get('part_number', ''),
                    item.get('category', ''),
                    item.get('current_stock', 0),
                    item.get('minimum_stock', 0),
                    item.get('unit_cost', 0.0)
                ])
        
        # Update maintenance tasks list
        if hasattr(self, 'tasks_list'):
            self.tasks_list.clear()
            for task in tasks_data:
                self.tasks_list.append([
                    task.get('vessel_name', ''),
                    task.get('system', ''),
                    task.get('task_name', ''),
                    task.get('next_due', ''),
                    task.get('status', ''),
                    task.get('priority', '')
                ])
        
        self._update_status(f"Data refreshed - {len(vessels_data)} vessels, {len(inventory_data)} items, {len(tasks_data)} tasks")
    
    def _update_status(self, message: str):
        """Update status bar"""
        def _update():
            status_bar = self.get_children()[-1]
            if isinstance(status_bar, Gtk.Statusbar):
                status_bar.pop(self.status_context_id)
                status_bar.push(self.status_context_id, message)
        
        self.ui_state_manager.queue_ui_action(_update)
    
    def do_delete_event(self, event) -> bool:
        """Enhanced window close event handler"""
        if not self._is_shutting_down:
            self._shutdown_sequence()
        return True
    
    def _shutdown_sequence(self):
        """Orderly application shutdown"""
        self._is_shutting_down = True
        
        # Save configuration
        try:
            x, y = self.get_position()
            width, height = self.get_size()
            self.config_manager.set("window_x", x)
            self.config_manager.set("window_y", y)
            self.config_manager.set("window_width", width)
            self.config_manager.set("window_height", height)
            self.config_manager.save()
        except Exception as e:
            logger.error(f"Failed to save configuration during shutdown: {e}")
        
        # Close resources
        try:
            self.data_loader.shutdown()
            self.db.close()
        except Exception as e:
            logger.error(f"Error during resource shutdown: {e}")
        
        logger.info("Vessel Keeper application shutdown complete")
        Gtk.main_quit()

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================
def show_error_dialog(parent: Optional[Gtk.Window], message: str):
    """Show error dialog safely"""
    def _show_dialog():
        dialog = Gtk.MessageDialog(
            transient_for=parent,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Error",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
    
    if parent and hasattr(parent, 'ui_state_manager'):
        parent.ui_state_manager.queue_ui_action(_show_dialog)
    else:
        GLib.idle_add(_show_dialog)

# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def main():
    """Professional main function with comprehensive error handling"""
    # Create necessary directories
    for directory in [REPORTS_DIR, 'backups', 'logs']:
        Path(directory).mkdir(exist_ok=True)
    
    # Global exception handling
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        # Use direct logging for critical errors during startup
        logging.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        
        try:
            dialog = Gtk.MessageDialog(
                transient_for=None,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="A critical error occurred",
            )
            dialog.format_secondary_text(
                f"The application encountered an unexpected error.\n\n"
                f"Error: {exc_value}\n\n"
                f"Please check the logs for details."
            )
            dialog.run()
            dialog.destroy()
        except Exception:
            pass
    
    sys.excepthook = global_exception_handler
    
    # Initialize and run application
    app = None
    try:
        app = VesselKeeperApp()
        app.connect("destroy", Gtk.main_quit)
        app.show_all()
        
        logger.info("Vessel Keeper started successfully")
        Gtk.main()
        
    except Exception as e:
        # Use direct logging for startup failures before logger is fully initialized
        logging.critical(f"Failed to start Vessel Keeper: {e}", exc_info=True)
        show_error_dialog(None, f"Failed to start application: {e}")
        sys.exit(1)
    
    finally:
        if app and hasattr(app, '_shutdown_sequence'):
            app._shutdown_sequence()

if __name__ == "__main__":
    main()
