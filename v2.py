import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib
import sys, os, sqlite3, logging, json, csv, datetime, threading
import shutil, contextvars, uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Callable, Iterator, TypedDict
from enum import Enum, auto
from contextlib import contextmanager
from queue import Queue, Empty, Full
import time
from pathlib import Path

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "vessel_keeper")
DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "vessel_keeper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DATABASE_FILE = os.path.join(DATA_DIR, "vessel_keeper.db")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MAX_SCHEMA_VERSION = 8
EXPORT_QUERIES = {
    'vessels': "SELECT * FROM vessels ORDER BY name",
    'inventory': "SELECT * FROM inventory_items ORDER BY name",
    'tasks': "SELECT mt.*,v.name vessel_name FROM maintenance_tasks mt LEFT JOIN vessels v ON mt.vessel_id=v.id ORDER BY mt.next_due",
    'trips': "SELECT tl.*,v.name vessel_name FROM trip_logs tl LEFT JOIN vessels v ON tl.vessel_id=v.id ORDER BY tl.departure DESC",
    'suppliers': "SELECT * FROM suppliers ORDER BY name",
    'stock_transactions': "SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id ORDER BY st.created_at DESC",
    'expenses': "SELECT e.*,v.name vessel_name FROM expenses e LEFT JOIN vessels v ON e.vessel_id=v.id ORDER BY e.expense_date DESC",
    'budgets': "SELECT b.*,v.name vessel_name FROM budgets b LEFT JOIN vessels v ON b.vessel_id=v.id ORDER BY b.fiscal_year DESC,b.category",
    'fuel_logs': "SELECT fl.*,v.name vessel_name,tl.destination trip_destination FROM fuel_logs fl LEFT JOIN vessels v ON fl.vessel_id=v.id LEFT JOIN trip_logs tl ON fl.trip_id=tl.id ORDER BY fl.created_at DESC",
    'audit_log': "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 1000",
    'crew': "SELECT * FROM crew_members ORDER BY last_name,first_name",
    'certifications': "SELECT c.*,cm.first_name||' '||cm.last_name crew_member FROM certifications c LEFT JOIN crew_members cm ON c.crew_member_id=cm.id ORDER BY cm.last_name,cm.first_name,c.expiry_date",
    'training': "SELECT t.*,cm.first_name||' '||cm.last_name crew_member FROM training_records t LEFT JOIN crew_members cm ON t.crew_member_id=cm.id ORDER BY cm.last_name,cm.first_name,t.date_completed DESC",
    'watch_schedules': "SELECT ws.*,cm.first_name||' '||cm.last_name crew_member,v.name vessel_name FROM watch_schedules ws LEFT JOIN crew_members cm ON ws.crew_member_id=cm.id LEFT JOIN vessels v ON ws.vessel_id=v.id ORDER BY ws.date,ws.start_time",
    'task_templates': "SELECT * FROM task_templates ORDER BY name",
    'task_parts': "SELECT tp.*,mt.work_order,mt.task_name,ii.name part_name FROM task_parts tp LEFT JOIN maintenance_tasks mt ON tp.task_id=mt.id LEFT JOIN inventory_items ii ON tp.item_id=ii.id ORDER BY mt.work_order",
}

request_id = contextvars.ContextVar('request_id', default='system')

class VesselRow(TypedDict, total=False): id: int; name: str; vessel_type: str; engine_make: str; engine_model: str; current_location: str; created_at: str
class MaintenanceTaskRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; system: str; task_name: str; description: str; next_due: str; status: str; priority: str; created_at: str
class InventoryItemRow(TypedDict, total=False): id: int; name: str; part_number: str; category: str; current_stock: int; minimum_stock: int; unit_cost: float; supplier_id: int; created_at: str
class TripLogRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; departure: str; arrival: str; destination: str; distance: float; fuel_used: float; created_at: str
class SupplierRow(TypedDict, total=False): id: int; name: str; contact: str; phone: str; email: str; preferred: int; created_at: str
class StockTransactionRow(TypedDict, total=False): id: int; item_id: int; item_name: str; quantity: int; transaction_type: str; notes: str; created_at: str
class ExpenseRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; trip_id: int; category: str; amount: float; currency: str; expense_date: str; description: str; vendor: str; receipt_path: str; tax_deductible: int; tax_category: str; created_at: str
class BudgetRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; fiscal_year: int; category: str; budget_amount: float; spent_amount: float; notes: str; created_at: str
class FuelLogRow(TypedDict, total=False): id: int; trip_id: int; vessel_id: int; vessel_name: str; gallons: float; cost_per_gallon: float; total_cost: float; fuel_type: str; vendor: str; location: str; engine_hours: float; created_at: str
class AuditEntryRow(TypedDict, total=False): id: int; timestamp: str; user: str; action: str; entity_type: str; entity_id: int; old_values: str; new_values: str; session_id: str
class CrewMemberRow(TypedDict, total=False): id: int; first_name: str; last_name: str; email: str; phone: str; emergency_contact: str; emergency_phone: str; role: str; date_joined: str; date_left: str; status: str; notes: str; created_at: str
class CertificationRow(TypedDict, total=False): id: int; crew_member_id: int; name: str; issuing_body: str; cert_number: str; category: str; issue_date: str; expiry_date: str; created_at: str
class TrainingRecordRow(TypedDict, total=False): id: int; crew_member_id: int; course_name: str; provider: str; date_completed: str; expiry_date: str; cost: float; notes: str; created_at: str
class WatchScheduleRow(TypedDict, total=False): id: int; crew_member_id: int; vessel_id: int; vessel_name: str; crew_name: str; date: str; start_time: str; end_time: str; role_on_watch: str; notes: str; created_at: str
class TaskPartRow(TypedDict, total=False): id: int; task_id: int; item_id: int; quantity_used: int; cost_per_unit: float; created_at: str
class TaskTemplateRow(TypedDict, total=False): id: int; name: str; system: str; description: str; priority: str; interval_days: int; interval_hours: float; default_vessel_type: str; created_at: str

class StructuredLogger:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_file = os.path.join(LOG_DIR, "vessel_keeper.log")
        logging.basicConfig(level=logging.INFO,
            format='%(asctime)s | %(name)-20s | %(levelname)-8s | %(threadName)-15s | %(message)s',
            handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler(sys.stdout)])
    def _log(self, level, msg, *args, **ctx):
        if args:
            msg = msg % args
        getattr(self.logger, level)("%s | request_id=%s%s", msg, request_id.get(), self._fmt(ctx))
    def info(self, msg, *args, **ctx):
        self._log("info", msg, *args, **ctx)
    def error(self, msg, *args, **ctx):
        self._log("error", msg, *args, **ctx)
    def warning(self, msg, *args, **ctx):
        self._log("warning", msg, *args, **ctx)
    def debug(self, msg, *args, **ctx):
        self._log("debug", msg, *args, **ctx)
    def critical(self, msg, *args, **ctx):
        self._log("critical", msg, *args, **ctx)
    def _fmt(self, ctx):
        if not ctx:
            return ""
        return " | " + " | ".join("%s=%s" % (k, v) for k, v in ctx.items())
logger = StructuredLogger()

# =============================================================================
# ENUMS, DATACLASSES, VALIDATION
# =============================================================================
class TaskStatus(Enum): PENDING=auto(); IN_PROGRESS=auto(); COMPLETED=auto(); OVERDUE=auto()
class Priority(Enum): LOW=auto(); MEDIUM=auto(); HIGH=auto(); CRITICAL=auto()
STATUS_MAP = {'pending':'Pending','in_progress':'In Progress','completed':'Completed','overdue':'Overdue'}
PRIORITY_MAP = {'low':'Low','medium':'Medium','high':'High','critical':'Critical'}

@dataclass
class Vessel:
    id: Optional[int] = None
    name: str = ""
    vessel_type: str = ""
    engine_make: str = ""
    engine_model: str = ""
    current_location: str = ""
    engine_hours: float = 0.0
    last_hours_update: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), name=d.get('name',''), vessel_type=d.get('vessel_type',''),
                   engine_make=d.get('engine_make',''), engine_model=d.get('engine_model',''),
                   current_location=d.get('current_location',''),
                   engine_hours=d.get('engine_hours', 0.0), last_hours_update=d.get('last_hours_update',''),
                   created_at=d.get('created_at',''))

@dataclass
class MaintenanceTask:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    system: str = ""
    task_name: str = ""
    description: str = ""
    next_due: str = ""
    status: str = "pending"
    priority: str = "medium"
    completed_at: str = ""
    completed_by: str = ""
    interval_days: Optional[int] = None
    interval_hours: Optional[float] = None
    is_recurring: bool = False
    work_order: str = ""
    completed_notes: str = ""
    cost: float = 0.0
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id',0), vessel_name=d.get('vessel_name',''),
                   system=d.get('system',''), task_name=d.get('task_name',''),
                   description=d.get('description',''), next_due=d.get('next_due',''),
                   status=d.get('status','pending'), priority=d.get('priority','medium'),
                   completed_at=d.get('completed_at',''), completed_by=d.get('completed_by',''),
                   interval_days=d.get('interval_days'), interval_hours=d.get('interval_hours'),
                   is_recurring=bool(d.get('is_recurring',0)), work_order=d.get('work_order',''),
                   completed_notes=d.get('completed_notes',''), cost=d.get('cost',0.0),
                   created_at=d.get('created_at',''))

@dataclass
class TripLog:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    departure: str = ""
    arrival: str = ""
    destination: str = ""
    distance: float = 0.0
    fuel_used: float = 0.0
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id',0), vessel_name=d.get('vessel_name',''),
                   departure=d.get('departure',''), arrival=d.get('arrival',''),
                   destination=d.get('destination',''), distance=d.get('distance',0.0),
                   fuel_used=d.get('fuel_used',0.0), created_at=d.get('created_at',''))

@dataclass
class Supplier:
    id: Optional[int] = None
    name: str = ""
    contact: str = ""
    phone: str = ""
    email: str = ""
    preferred: bool = False
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), name=d.get('name',''), contact=d.get('contact',''),
                   phone=d.get('phone',''), email=d.get('email',''),
                   preferred=bool(d.get('preferred',0)), created_at=d.get('created_at',''))

@dataclass
class Expense:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    trip_id: Optional[int] = None
    category: str = ""
    amount: float = 0.0
    currency: str = "USD"
    expense_date: str = ""
    description: str = ""
    vendor: str = ""
    receipt_path: str = ""
    tax_deductible: bool = False
    tax_category: str = ""
    gst_rate: float = 0.0
    gst_amount: float = 0.0
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0), vessel_name=d.get('vessel_name', ''),
                   trip_id=d.get('trip_id'), category=d.get('category', ''), amount=d.get('amount', 0.0),
                   currency=d.get('currency', 'USD'), expense_date=d.get('expense_date', ''),
                   description=d.get('description', ''), vendor=d.get('vendor', ''),
                   receipt_path=d.get('receipt_path', ''), tax_deductible=bool(d.get('tax_deductible', 0)),
                   tax_category=d.get('tax_category', ''), gst_rate=d.get('gst_rate', 0.0),
                   gst_amount=d.get('gst_amount', 0.0), created_at=d.get('created_at', ''))

@dataclass
class Budget:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    fiscal_year: int = 0
    category: str = ""
    budget_amount: float = 0.0
    spent_amount: float = 0.0
    notes: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0), vessel_name=d.get('vessel_name', ''),
                   fiscal_year=d.get('fiscal_year', 0), category=d.get('category', ''),
                   budget_amount=d.get('budget_amount', 0.0), spent_amount=d.get('spent_amount', 0.0),
                   notes=d.get('notes', ''), created_at=d.get('created_at', ''))

@dataclass
class FuelLog:
    id: Optional[int] = None
    trip_id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    gallons: float = 0.0
    cost_per_gallon: float = 0.0
    total_cost: float = 0.0
    fuel_type: str = ""
    vendor: str = ""
    location: str = ""
    engine_hours: float = 0.0
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), trip_id=d.get('trip_id'), vessel_id=d.get('vessel_id', 0),
                   vessel_name=d.get('vessel_name', ''), gallons=d.get('gallons', 0.0),
                   cost_per_gallon=d.get('cost_per_gallon', 0.0), total_cost=d.get('total_cost', 0.0),
                   fuel_type=d.get('fuel_type', ''), vendor=d.get('vendor', ''),
                   location=d.get('location', ''), engine_hours=d.get('engine_hours', 0.0),
                   created_at=d.get('created_at', ''))

@dataclass
class CrewMember:
    id: Optional[int] = None
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    emergency_contact: str = ""
    emergency_phone: str = ""
    role: str = ""
    date_joined: str = ""
    date_left: str = ""
    status: str = "active"
    notes: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), first_name=d.get('first_name', ''), last_name=d.get('last_name', ''),
                   email=d.get('email', ''), phone=d.get('phone', ''),
                   emergency_contact=d.get('emergency_contact', ''), emergency_phone=d.get('emergency_phone', ''),
                   role=d.get('role', ''), date_joined=d.get('date_joined', ''),
                   date_left=d.get('date_left', ''), status=d.get('status', 'active'),
                   notes=d.get('notes', ''), created_at=d.get('created_at', ''))

@dataclass
class Certification:
    id: Optional[int] = None
    crew_member_id: int = 0
    name: str = ""
    issuing_body: str = ""
    cert_number: str = ""
    category: str = ""
    issue_date: str = ""
    expiry_date: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0), name=d.get('name', ''),
                   issuing_body=d.get('issuing_body', ''), cert_number=d.get('cert_number', ''),
                   category=d.get('category', ''), issue_date=d.get('issue_date', ''),
                   expiry_date=d.get('expiry_date', ''), created_at=d.get('created_at', ''))

@dataclass
class TrainingRecord:
    id: Optional[int] = None
    crew_member_id: int = 0
    course_name: str = ""
    provider: str = ""
    date_completed: str = ""
    expiry_date: str = ""
    cost: float = 0.0
    notes: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0), course_name=d.get('course_name', ''),
                   provider=d.get('provider', ''), date_completed=d.get('date_completed', ''),
                   expiry_date=d.get('expiry_date', ''), cost=d.get('cost', 0.0),
                   notes=d.get('notes', ''), created_at=d.get('created_at', ''))

@dataclass
class WatchSchedule:
    id: Optional[int] = None
    crew_member_id: int = 0
    vessel_id: int = 0
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    role_on_watch: str = ""
    notes: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0), vessel_id=d.get('vessel_id', 0),
                   date=d.get('date', ''), start_time=d.get('start_time', ''),
                   end_time=d.get('end_time', ''), role_on_watch=d.get('role_on_watch', ''),
                   notes=d.get('notes', ''), created_at=d.get('created_at', ''))

@dataclass
class TaskPart:
    id: Optional[int] = None
    task_id: int = 0
    item_id: int = 0
    quantity_used: int = 1
    cost_per_unit: float = 0.0
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), task_id=d.get('task_id', 0), item_id=d.get('item_id', 0),
                   quantity_used=d.get('quantity_used', 1), cost_per_unit=d.get('cost_per_unit', 0.0),
                   created_at=d.get('created_at', ''))

@dataclass
class TaskTemplate:
    id: Optional[int] = None
    name: str = ""
    system: str = ""
    description: str = ""
    priority: str = "medium"
    interval_days: Optional[int] = None
    interval_hours: Optional[float] = None
    default_vessel_type: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), name=d.get('name', ''), system=d.get('system', ''),
                   description=d.get('description', ''), priority=d.get('priority', 'medium'),
                   interval_days=d.get('interval_days'), interval_hours=d.get('interval_hours'),
                   default_vessel_type=d.get('default_vessel_type', ''), created_at=d.get('created_at', ''))

class DatabaseError(Exception): pass
class ValidationError(Exception): pass
class ResourceError(Exception): pass
class SecurityError(Exception): pass

class DataValidator:
    @staticmethod
    def required(val, field): stripped = val.strip(); return stripped if stripped else (_ for _ in ()).throw(ValidationError(f"{field} cannot be empty"))
    @staticmethod
    def maxlen(val, field, mx=255): return val if len(val) <= mx else (_ for _ in ()).throw(ValidationError(f"{field} too long (max {mx})"))
    @staticmethod
    def validate_vessel(v):
        DataValidator.required(v.name, "Vessel name"); DataValidator.maxlen(v.name, "Vessel name")
        DataValidator.maxlen(v.vessel_type, "Vessel type", 100); DataValidator.maxlen(v.engine_make, "Engine make", 100); DataValidator.maxlen(v.engine_model, "Engine model", 100)
    @staticmethod
    def validate_task(t):
        if t.vessel_id <= 0: raise ValidationError("Select a vessel")
        DataValidator.required(t.task_name, "Task name"); DataValidator.maxlen(t.task_name, "Task name")
        if t.status not in ('pending','in_progress','completed','overdue'): raise ValidationError(f"Invalid status: {t.status}")
        if t.priority not in ('low','medium','high','critical'): raise ValidationError(f"Invalid priority: {t.priority}")
    @staticmethod
    def validate_stock(cur, mini):
        if cur < 0: raise ValidationError("Stock cannot be negative")
        if mini < 0: raise ValidationError("Minimum stock cannot be negative")
    @staticmethod
    def validate_item_name(n):
        s = n.strip()
        if not s: raise ValidationError("Item name cannot be empty")
        return s
    @staticmethod
    def validate_supplier(s):
        DataValidator.required(s.name, "Supplier name"); DataValidator.maxlen(s.name, "Supplier name")
    @staticmethod
    def validate_crew_member(c):
        DataValidator.required(c.first_name, "First name"); DataValidator.maxlen(c.first_name, "First name", 100)
        DataValidator.required(c.last_name, "Last name"); DataValidator.maxlen(c.last_name, "Last name", 100)
        DataValidator.required(c.role, "Role"); DataValidator.maxlen(c.role, "Role", 100)
        if c.status not in ('active', 'inactive', 'terminated'):
            raise ValidationError("Invalid crew status")
    @staticmethod
    def validate_certification(c):
        DataValidator.required(c.name, "Certification name"); DataValidator.maxlen(c.name, "Certification name")
        if c.crew_member_id <= 0: raise ValidationError("Select a crew member")
    @staticmethod
    def validate_training(t):
        DataValidator.required(t.course_name, "Course name"); DataValidator.maxlen(t.course_name, "Course name")
        if t.crew_member_id <= 0: raise ValidationError("Select a crew member")
    @staticmethod
    def validate_watch_schedule(w):
        if w.crew_member_id <= 0: raise ValidationError("Select a crew member")
        DataValidator.required(w.date, "Date"); DataValidator.required(w.start_time, "Start time")
        DataValidator.required(w.end_time, "End time")
    @staticmethod
    def validate_task_part(tp):
        if tp.task_id <= 0: raise ValidationError("Select a task")
        if tp.item_id <= 0: raise ValidationError("Select an inventory item")
        if tp.quantity_used < 1: raise ValidationError("Quantity must be at least 1")
    @staticmethod
    def validate_template(t):
        DataValidator.required(t.name, "Template name"); DataValidator.maxlen(t.name, "Template name")

# =============================================================================
# CONFIGURATION
# =============================================================================
class AppConfig:
    def __init__(self):
        self.window_x=100; self.window_y=100; self.window_width=1200; self.window_height=800
        self.due_soon_days=7; self.backup_retention_days=30; self.auto_backup=True
        self.backup_interval_hours=24
    def items(self): return ((k,getattr(self,k)) for k in dir(self) if not k.startswith('_') and not callable(getattr(self,k)))

class ConfigManager:
    def __init__(self, cf=CONFIG_FILE):
        self.config_file=cf; self._config=AppConfig(); self._lock=threading.RLock(); self._load_config()
    def _load_config(self):
        try:
            with open(self.config_file) as f:
                uc = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Config load failed: %s", e)
            return
        try:
            with self._lock:
                for k, v in uc.items():
                    if hasattr(self._config, k):
                        setattr(self._config, k, v)
        except Exception as e:
            logger.warning("Config apply failed: %s", e)
    def get(self,k,d=None):
        with self._lock: return getattr(self._config,k,d)
    def set(self,k,v):
        with self._lock:
            if hasattr(self._config,k): setattr(self._config,k,v)
    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = "%s.tmp" % self.config_file
            with open(tmp, 'w') as f:
                json.dump(dict(self._config.items()), f, indent=2)
            os.replace(tmp, self.config_file)
        except IOError as e:
            logger.error("Config save failed: %s", e)
            raise

# =============================================================================
# DATABASE
# =============================================================================
class DatabaseConnectionPool:
    def __init__(self, db_path, max_connections=3, timeout=30.0):
        self.db_path=db_path; self.max_connections=max_connections; self.timeout=timeout
        self._connections=Queue(max_connections); self._in_use=set(); self._lock=threading.RLock(); self._closed=False
        for _ in range(max_connections): self._connections.put(self._create_connection())
    def _create_connection(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL"); conn.execute("PRAGMA cache_size=-64000"); conn.execute("PRAGMA temp_store=MEMORY")
            return conn
        except sqlite3.Error as e: raise DatabaseError("DB connection failed: %s" % e) from e
    def _health_check(self, conn):
        try: conn.execute("SELECT 1"); return True
        except sqlite3.Error: return False
    @contextmanager
    def get_connection(self):
        if self._closed: raise DatabaseError("Pool closed")
        conn=None
        try:
            conn=self._acquire(); yield conn; conn.commit()
        except sqlite3.Error as e:
            if conn: conn.rollback()
            raise DatabaseError(f"DB error: {e}") from e
        except Exception:
            if conn: conn.rollback()
            raise
        finally:
            if conn: self._release(conn)
    def _acquire(self):
        deadline=time.monotonic()+self.timeout; delay=0.05
        while True:
            rem=deadline-time.monotonic()
            if rem<=0: break
            try:
                conn=self._connections.get(timeout=min(delay,rem))
                if self._health_check(conn):
                    with self._lock:
                        if not self._closed: self._in_use.add(id(conn)); return conn
                conn.close()
            except Empty: delay=min(delay*1.5,1.0)
        raise DatabaseError(f"Timeout after {self.timeout}s")
    def _release(self, conn):
        try:
            with self._lock: self._in_use.discard(id(conn))
            if not self._closed and self._health_check(conn): self._connections.put(conn,timeout=1)
            else:
                conn.close()
                if not self._closed: self._connections.put(self._create_connection(),timeout=1)
        except (Full, sqlite3.Error):
            try:
                conn.close()
            except Exception:
                pass
    def close_all(self):
        self._closed = True
        with self._lock:
            conns = []
            while not self._connections.empty():
                try:
                    c = self._connections.get_nowait()
                    conns.append(c)
                except Empty:
                    break
            for c in conns:
                try:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
            self._in_use.clear()

class DatabaseMetrics:
    def __init__(self):
        self._lock=threading.RLock(); self._q=0; self._e=0; self._t=0.0; self._slow=1.0
    def record_success(self,d): 
        with self._lock: self._q+=1; self._t+=d
        if d > self._slow:
            logger.warning("Slow query: %.3fs", d)
    def record_error(self,d): 
        with self._lock: self._e+=1; self._t+=d
    def get_summary(self):
        with self._lock:
            avg=self._t/self._q if self._q else 0; er=self._e/self._q*100 if self._q else 0
            return {"total_queries":self._q,"error_count":self._e,"error_rate_pct":er,"avg_query_time_s":avg,"total_query_time_s":self._t}

class ThreadSafeDatabase:
    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        self.connection_pool = DatabaseConnectionPool(db_path, max_connections=3)
        self._qlock = threading.RLock()
        self._metrics = DatabaseMetrics()
        self._vcache: Dict[int, str] = {}
        self._vlock = threading.RLock()
        self._ensure()

    @staticmethod
    def _validate_backup_path(path: str) -> str:
        p = Path(path)
        allowed = Path(BACKUP_DIR)
        try:
            p.relative_to(allowed)
        except ValueError:
            try:
                p.relative_to(allowed / "emergency")
            except ValueError:
                raise ValidationError(
                    "Backup path must be under %s" % allowed
                )
        if "'" in str(p):
            raise ValidationError("Invalid characters in backup path")
        return str(p)

    def _ensure(self):
        logger.info("DB integrity check")
        if not self._verify(): self._init_new(); return
        if not self._integrity_check()["healthy"]: self._handle_corruption()
        self._migrate()
        self.prune_old_data()

    def _verify(self):
        if not os.path.exists(self.db_path): return False
        if os.stat(self.db_path).st_size==0: return False
        try:
            with open(self.db_path,'rb') as f:
                if not f.read(16).startswith(b'SQLite format 3'): return False
        except (OSError, IOError):
            return False
        return True

    def _integrity_check(self):
        r={"healthy":False,"errors":[]}
        try:
            with self.connection_pool.get_connection() as conn:
                q=conn.execute("PRAGMA quick_check").fetchone()
                if q[0]!="ok":
                    r["errors"].append(f"Check failed: {q[0]}")
                    full=conn.execute("PRAGMA integrity_check").fetchall()
                    r["errors"].extend(row[0] for row in full if row[0]!="ok")
                    return r
                fk=conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk: r["errors"].extend(f"FK violation: {row[0]}/{row[1]}" for row in fk)
                r["healthy"]=len(r["errors"])==0
        except sqlite3.Error as e:
            r["errors"].append(str(e))
        return r

    def _handle_corruption(self):
        bp = self._create_emergency_backup("corruption")
        try:
            with self.connection_pool.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        except Exception:
            pass
        if not self._restore_backup():
            if bp and self._restore_specific(bp): return
            self._init_new()

    def _create_emergency_backup(self, reason: str):
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S%f")
            d = Path(BACKUP_DIR) / "emergency"
            d.mkdir(parents=True, exist_ok=True)
            bp = d / "emergency_%s_%s.db" % (reason, ts)
            bp_str = self._validate_backup_path(str(bp))
            with self.connection_pool.get_connection() as conn:
                conn.execute("VACUUM INTO ?", (bp_str,))
            with sqlite3.connect(str(bp)) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    bp.unlink(missing_ok=True)
                    return None
            return str(bp)
        except Exception:
            return None

    def _migrate(self):
        try:
            with self.connection_pool.get_connection() as conn:
                cv=conn.execute("PRAGMA user_version").fetchone()[0]
                needed=[(v,sql) for v,sql in sorted(self._migrations().items()) if v>cv]
                if not needed: return
            # Best-effort pre-migration backup; proceed even if it fails
            try:
                bp = self._create_emergency_backup("pre_migration")
            except Exception:
                bp = None
            with self.connection_pool.get_connection() as conn:
                # Temporarily disable FK checks during migration to allow table reordering
                conn.execute("PRAGMA foreign_keys=OFF")
                for v, sql in needed:
                    conn.executescript(sql)
                    conn.execute("PRAGMA user_version=%d" % v)
                conn.execute("PRAGMA foreign_keys=ON")
        except Exception as e:
            logger.error("Migration failed: %s", e)
            if bp:
                self._restore_specific(bp)
            raise

    def _migrations(self):
        return {
            1: """
CREATE TABLE IF NOT EXISTS vessels (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,vessel_type TEXT,engine_make TEXT,engine_model TEXT,current_location TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,contact TEXT,phone TEXT,email TEXT,preferred INTEGER DEFAULT 0,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS inventory_items (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,part_number TEXT,category TEXT,current_stock INTEGER DEFAULT 0,minimum_stock INTEGER DEFAULT 0,unit_cost REAL,supplier_id INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(supplier_id) REFERENCES suppliers(id));
CREATE TABLE IF NOT EXISTS maintenance_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,vessel_id INTEGER NOT NULL,system TEXT,task_name TEXT NOT NULL,description TEXT,next_due TEXT,status TEXT DEFAULT 'pending',priority TEXT DEFAULT 'medium',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(vessel_id) REFERENCES vessels(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS trip_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,vessel_id INTEGER NOT NULL,departure TEXT,arrival TEXT,destination TEXT,distance REAL,fuel_used REAL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(vessel_id) REFERENCES vessels(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS stock_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,item_id INTEGER NOT NULL,quantity INTEGER,transaction_type TEXT,notes TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(item_id) REFERENCES inventory_items(id) ON DELETE CASCADE);
""",
            2: """
CREATE INDEX IF NOT EXISTS idx_mt_vessel ON maintenance_tasks(vessel_id); CREATE INDEX IF NOT EXISTS idx_mt_status ON maintenance_tasks(status);
CREATE INDEX IF NOT EXISTS idx_ii_supplier ON inventory_items(supplier_id); CREATE INDEX IF NOT EXISTS idx_tl_vessel ON trip_logs(vessel_id);
CREATE INDEX IF NOT EXISTS idx_st_item ON stock_transactions(item_id);
""",
            3: """
ALTER TABLE maintenance_tasks ADD COLUMN completed_at TEXT;
ALTER TABLE maintenance_tasks ADD COLUMN completed_by TEXT;
""",
            4: """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id INTEGER REFERENCES vessels(id) ON DELETE SET NULL,
    trip_id INTEGER REFERENCES trip_logs(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    expense_date TEXT NOT NULL,
    description TEXT,
    vendor TEXT,
    receipt_path TEXT,
    tax_deductible INTEGER DEFAULT 0,
    tax_category TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
    fiscal_year INTEGER NOT NULL,
    category TEXT NOT NULL,
    budget_amount REAL NOT NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vessel_id, fiscal_year, category)
);
CREATE TABLE IF NOT EXISTS fuel_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER REFERENCES trip_logs(id) ON DELETE SET NULL,
    vessel_id INTEGER NOT NULL REFERENCES vessels(id) ON DELETE CASCADE,
    gallons REAL NOT NULL,
    cost_per_gallon REAL,
    total_cost REAL,
    fuel_type TEXT,
    vendor TEXT,
    location TEXT,
    engine_hours REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""",
            5: """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user TEXT DEFAULT 'system',
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    old_values TEXT,
    new_values TEXT,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_expenses_vessel ON expenses(vessel_id);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_fuel_logs_vessel ON fuel_logs(vessel_id);
CREATE INDEX IF NOT EXISTS idx_budgets_vessel ON budgets(vessel_id);
""",
            6: """
ALTER TABLE expenses ADD COLUMN gst_rate REAL DEFAULT 0.0;
ALTER TABLE expenses ADD COLUMN gst_amount REAL DEFAULT 0.0;
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO app_settings(key, value) VALUES
    ('schema_version', '6'),
    ('company_name', ''),
    ('currency', 'USD'),
    ('fiscal_year_start', '01-01'),
    ('gst_rate', '0.0'),
    ('audit_retention_days', '365'),
    ('session_timeout_minutes', '30'),
    ('require_pin', 'false'),
    ('pin_hash', ''),
    ('data_retention_days', '730'),
    ('encryption_enabled', 'false');
""",
        7: """
CREATE TABLE IF NOT EXISTS crew_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    emergency_contact TEXT,
    emergency_phone TEXT,
    role TEXT NOT NULL,
    date_joined TEXT,
    date_left TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS certifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crew_member_id INTEGER NOT NULL REFERENCES crew_members(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    issuing_body TEXT,
    cert_number TEXT,
    category TEXT,
    issue_date TEXT,
    expiry_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS training_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crew_member_id INTEGER NOT NULL REFERENCES crew_members(id) ON DELETE CASCADE,
    course_name TEXT NOT NULL,
    provider TEXT,
    date_completed TEXT,
    expiry_date TEXT,
    cost REAL DEFAULT 0.0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS watch_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crew_member_id INTEGER NOT NULL REFERENCES crew_members(id) ON DELETE CASCADE,
    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    role_on_watch TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cert_crew ON certifications(crew_member_id);
CREATE INDEX IF NOT EXISTS idx_cert_expiry ON certifications(expiry_date);
CREATE INDEX IF NOT EXISTS idx_train_crew ON training_records(crew_member_id);
CREATE INDEX IF NOT EXISTS idx_watch_crew ON watch_schedules(crew_member_id);
CREATE INDEX IF NOT EXISTS idx_watch_date ON watch_schedules(date);
""",
        8: """
ALTER TABLE maintenance_tasks ADD COLUMN interval_days INTEGER;
ALTER TABLE maintenance_tasks ADD COLUMN interval_hours REAL;
ALTER TABLE maintenance_tasks ADD COLUMN is_recurring INTEGER DEFAULT 0;
ALTER TABLE maintenance_tasks ADD COLUMN work_order TEXT;
ALTER TABLE maintenance_tasks ADD COLUMN completed_notes TEXT;
ALTER TABLE maintenance_tasks ADD COLUMN cost REAL DEFAULT 0.0;
ALTER TABLE vessels ADD COLUMN engine_hours REAL DEFAULT 0.0;
ALTER TABLE vessels ADD COLUMN last_hours_update TEXT;
CREATE TABLE IF NOT EXISTS task_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES maintenance_tasks(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE RESTRICT,
    quantity_used INTEGER NOT NULL DEFAULT 1,
    cost_per_unit REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS task_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    system TEXT,
    description TEXT,
    priority TEXT DEFAULT 'medium',
    interval_days INTEGER,
    interval_hours REAL,
    default_vessel_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_taskparts_task ON task_parts(task_id);
-- Update schema version in app_settings
UPDATE app_settings SET value='8' WHERE key='schema_version';
""",
        }

    def _init_new(self):
        self.connection_pool.close_all()
        if os.path.exists(self.db_path): os.remove(self.db_path)
        self.connection_pool=DatabaseConnectionPool(self.db_path,max_connections=3)
        with self.connection_pool.get_connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            for v,sql in sorted(self._migrations().items()):
                conn.executescript(sql)
                conn.execute("PRAGMA user_version=%d" % v)
            conn.execute("PRAGMA foreign_keys=ON")

    def _restore_backup(self):
        try:
            backup_dir = Path(BACKUP_DIR)
            if not backup_dir.exists():
                return False
            backups = sorted(backup_dir.rglob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            for c in backups:
                try:
                    with sqlite3.connect(str(c)) as t:
                        if t.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                            self.connection_pool.close_all()
                            shutil.copy2(str(c), self.db_path)
                            self.connection_pool = DatabaseConnectionPool(self.db_path, max_connections=3)
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _restore_specific(self, p: str) -> bool:
        try:
            self.connection_pool.close_all()
            shutil.copy2(p, self.db_path)
            self.connection_pool = DatabaseConnectionPool(self.db_path, max_connections=3)
            return True
        except Exception:
            return False

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        st = time.time()
        with self._qlock, self.connection_pool.get_connection() as conn:
            c = conn.cursor()
            try:
                yield c
                self._metrics.record_success(time.time() - st)
            except sqlite3.Error as e:
                self._metrics.record_error(time.time() - st)
                raise
            except Exception:
                self._metrics.record_error(time.time() - st)
                raise

    def q(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self.cursor() as c:
            c.execute(query, params)
            return [dict(r) for r in c.fetchall()]

    def u(self, query: str, params: tuple = ()) -> int:
        with self.cursor() as c:
            c.execute(query, params)
            return c.rowcount

    def ins(self, query: str, params: tuple = ()) -> int:
        with self.cursor() as c:
            c.execute(query, params)
            return c.lastrowid

    # ---- Vessels ----
    def get_vessels(self) -> List[Dict[str, Any]]:
        with self._vlock:
            r = self.q("SELECT * FROM vessels ORDER BY name")
            self._vcache = {row['id']: row['name'] for row in r}
        return r
    def get_vessel(self, vid):
        r=self.q("SELECT * FROM vessels WHERE id=?",(vid,)); return r[0] if r else None
    def add_vessel(self,v):
        DataValidator.validate_vessel(v)
        rid=self.ins("INSERT INTO vessels(name,vessel_type,engine_make,engine_model,current_location) VALUES(?,?,?,?,?)",
            (v.name.strip(),v.vessel_type.strip(),v.engine_make.strip(),v.engine_model.strip(),v.current_location.strip()))
        with self._vlock: self._vcache[rid]=v.name.strip()
        self.audit("create", "vessel", rid, new_values=v.__dict__)
        return rid
    def update_vessel(self,v):
        DataValidator.validate_vessel(v)
        old=self.get_vessel(v.id)
        rc=self.u("UPDATE vessels SET name=?,vessel_type=?,engine_make=?,engine_model=?,current_location=? WHERE id=?",
            (v.name.strip(),v.vessel_type.strip(),v.engine_make.strip(),v.engine_model.strip(),v.current_location.strip(),v.id))
        with self._vlock: self._vcache[v.id]=v.name.strip()
        if rc: self.audit("update", "vessel", v.id, old_values=old, new_values=v.__dict__)
        return rc
    def delete_vessel(self,vid):
        old=self.get_vessel(vid)
        rc=self.u("DELETE FROM vessels WHERE id=?",(vid,))
        with self._vlock: self._vcache.pop(vid,None)
        if rc: self.audit("delete", "vessel", vid, old_values=old)
        return rc
    def vessel_count(self):
        r=self.q("SELECT COUNT(*) c FROM vessels"); return r[0]['c'] if r else 0

    # ---- Maintenance ----
    def get_tasks_with_vessels(self):
        with self._vlock:
            if not self._vcache: self.get_vessels()
        tasks=self.q("SELECT * FROM maintenance_tasks ORDER BY next_due")
        with self._vlock:
            for t in tasks: t['vessel_name']=self._vcache.get(t['vessel_id'],'Unknown')
        return tasks
    def get_tasks(self): return self.get_tasks_with_vessels()
    def get_task(self, tid): tasks=self.get_tasks_with_vessels(); return next((t for t in tasks if t['id']==tid), None)
    def add_task(self, t):
        DataValidator.validate_task(t)
        rid=self.ins("""INSERT INTO maintenance_tasks(vessel_id,system,task_name,description,next_due,status,priority,
            interval_days,interval_hours,is_recurring,work_order,cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.vessel_id,t.system.strip(),t.task_name.strip(),t.description.strip(),t.next_due.strip(),
             t.status,t.priority,t.interval_days,t.interval_hours,int(t.is_recurring),t.work_order,t.cost))
        self.audit("create", "task", rid, new_values=t.__dict__)
        return rid
    def update_task(self, t):
        DataValidator.validate_task(t)
        old=self.get_task(t.id)
        rc=self.u("""UPDATE maintenance_tasks SET vessel_id=?,system=?,task_name=?,description=?,next_due=?,status=?,priority=?,
            interval_days=?,interval_hours=?,is_recurring=?,work_order=?,cost=? WHERE id=?""",
            (t.vessel_id,t.system.strip(),t.task_name.strip(),t.description.strip(),t.next_due.strip(),
             t.status,t.priority,t.interval_days,t.interval_hours,int(t.is_recurring),t.work_order,t.cost,t.id))
        if rc: self.audit("update", "task", t.id, old_values=dict(old) if old else None, new_values=t.__dict__)
        return rc
    def delete_task(self,tid):
        old=self.get_task(tid)
        rc=self.u("DELETE FROM maintenance_tasks WHERE id=?",(tid,))
        if rc: self.audit("delete", "task", tid, old_values=dict(old) if old else None)
        return rc
    def task_counts(self, due_soon_days=7):
        self.check_overdue_tasks()
        r=self.q("SELECT status,COUNT(*) c FROM maintenance_tasks GROUP BY status"); counts={row['status']:row['c'] for row in r}
        due=self.q("SELECT COUNT(*) c FROM maintenance_tasks WHERE status='pending' AND next_due IS NOT NULL AND next_due<=date('now', ?)", ('+%d days' % due_soon_days,))
        return {'total':sum(counts.values()),'pending':counts.get('pending',0),'in_progress':counts.get('in_progress',0),'completed':counts.get('completed',0),'overdue':counts.get('overdue',0),'due_soon':due[0]['c'] if due else 0}

    # ---- Inventory ----
    def get_items(self): return self.q("SELECT * FROM inventory_items ORDER BY name")
    def get_item(self, iid):
        r=self.q("SELECT * FROM inventory_items WHERE id=?",(iid,)); return r[0] if r else None
    def add_item(self,name,pn,cat,stock,minstock,cost):
        name=DataValidator.validate_item_name(name); DataValidator.validate_stock(stock,minstock)
        rid=self.ins("INSERT INTO inventory_items(name,part_number,category,current_stock,minimum_stock,unit_cost) VALUES(?,?,?,?,?,?)",
            (name,pn.strip(),cat.strip(),stock,minstock,cost))
        self.audit("create", "inventory_item", rid, new_values={"name":name,"part_number":pn,"category":cat,"stock":stock,"minstock":minstock,"cost":cost})
        return rid
    def update_item(self,iid,name,pn,cat,stock,minstock,cost):
        old=self.get_item(iid)
        name=DataValidator.validate_item_name(name); DataValidator.validate_stock(stock,minstock)
        rc=self.u("UPDATE inventory_items SET name=?,part_number=?,category=?,current_stock=?,minimum_stock=?,unit_cost=? WHERE id=?",
            (name,pn.strip(),cat.strip(),stock,minstock,cost,iid))
        if rc: self.audit("update", "inventory_item", iid, old_values=old, new_values={"name":name,"part_number":pn,"category":cat,"stock":stock,"minstock":minstock,"cost":cost})
        return rc
    def delete_item(self,iid):
        old=self.get_item(iid)
        rc=self.u("DELETE FROM inventory_items WHERE id=?",(iid,))
        if rc: self.audit("delete", "inventory_item", iid, old_values=old)
        return rc
    def adjust_stock(self,iid,delta,notes=""):
        item=self.get_item(iid)
        if not item: raise ValidationError("Item not found")
        new_stock=item['current_stock']+delta
        if new_stock<0: raise ValidationError("Insufficient stock (have %d, need %d)" % (item['current_stock'], -delta))
        self.u("UPDATE inventory_items SET current_stock=? WHERE id=?",(new_stock,iid))
        tx_type="add" if delta>0 else "remove"
        self.ins("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,?,?)",(iid,abs(delta),tx_type,notes.strip()))
        self.audit("stock_adjust", "inventory_item", iid, new_values={"current_stock":new_stock,"delta":delta,"notes":notes})
        return new_stock
    def inventory_summary(self):
        items=self.get_items(); low=[i for i in items if i['current_stock']<=i['minimum_stock']]
        return {'total':len(items),'low_count':len(low),'low_items':low,'total_value':sum(i['current_stock']*(i['unit_cost'] or 0) for i in items)}

    # ---- Trip Logs ----
    def get_trips(self):
        trips=self.q("SELECT * FROM trip_logs ORDER BY departure DESC")
        with self._vlock:
            if not self._vcache: self.get_vessels()
            for t in trips: t['vessel_name']=self._vcache.get(t['vessel_id'],'Unknown')
        return trips
    def add_trip(self,t):
        if t.vessel_id<=0: raise ValidationError("Select a vessel")
        rid=self.ins("INSERT INTO trip_logs(vessel_id,departure,arrival,destination,distance,fuel_used) VALUES(?,?,?,?,?,?)",
            (t.vessel_id,t.departure.strip(),t.arrival.strip(),t.destination.strip(),t.distance,t.fuel_used))
        self.audit("create", "trip", rid, new_values=t.__dict__)
        return rid
    def update_trip(self,t):
        if t.vessel_id<=0: raise ValidationError("Select a vessel")
        old=self.q("SELECT * FROM trip_logs WHERE id=?",(t.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE trip_logs SET vessel_id=?,departure=?,arrival=?,destination=?,distance=?,fuel_used=? WHERE id=?",
            (t.vessel_id,t.departure.strip(),t.arrival.strip(),t.destination.strip(),t.distance,t.fuel_used,t.id))
        if rc: self.audit("update", "trip", t.id, old_values=old, new_values=t.__dict__)
        return rc
    def delete_trip(self,tid):
        old=self.q("SELECT * FROM trip_logs WHERE id=?",(tid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM trip_logs WHERE id=?",(tid,))
        if rc: self.audit("delete", "trip", tid, old_values=old)
        return rc

    # ---- Suppliers ----
    def get_suppliers(self): return self.q("SELECT * FROM suppliers ORDER BY name")
    def add_supplier(self,s):
        DataValidator.validate_supplier(s)
        rid=self.ins("INSERT INTO suppliers(name,contact,phone,email,preferred) VALUES(?,?,?,?,?)",
            (s.name.strip(),s.contact.strip(),s.phone.strip(),s.email.strip(),1 if s.preferred else 0))
        self.audit("create", "supplier", rid, new_values=s.__dict__)
        return rid
    def update_supplier(self,s):
        DataValidator.validate_supplier(s)
        old=self.q("SELECT * FROM suppliers WHERE id=?",(s.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE suppliers SET name=?,contact=?,phone=?,email=?,preferred=? WHERE id=?",
            (s.name.strip(),s.contact.strip(),s.phone.strip(),s.email.strip(),1 if s.preferred else 0,s.id))
        if rc: self.audit("update", "supplier", s.id, old_values=old, new_values=s.__dict__)
        return rc
    def delete_supplier(self,sid):
        old=self.q("SELECT * FROM suppliers WHERE id=?",(sid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM suppliers WHERE id=?",(sid,))
        if rc: self.audit("delete", "supplier", sid, old_values=old)
        return rc

    # ---- Stock Transactions ----
    def get_transactions(self,iid=None):
        if iid: rows=self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id WHERE st.item_id=? ORDER BY st.created_at DESC",(iid,))
        else: rows=self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id ORDER BY st.created_at DESC")
        return rows

    # ---- Backup ----
    def create_backup(self, path: str) -> bool:
        try:
            vpath = self._validate_backup_path(path)
            with self.connection_pool.get_connection() as conn:
                conn.execute("VACUUM INTO ?", (vpath,))
            with sqlite3.connect(path) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0] == "ok":
                    return True
                os.remove(path)
                return False
        except (sqlite3.Error, OSError, ValidationError):
            return False

    # ---- Audit Log ----
    def audit(self, action: str, entity_type: str = None, entity_id: int = None,
              old_values: dict = None, new_values: dict = None, session_id: str = None):
        try:
            self.ins("INSERT INTO audit_log(action,entity_type,entity_id,old_values,new_values,session_id) VALUES(?,?,?,?,?,?)",
                     (action, entity_type, entity_id,
                      json.dumps(old_values) if old_values else None,
                      json.dumps(new_values) if new_values else None,
                      session_id or request_id.get()))
        except Exception as e:
            logger.warning("Audit write failed: %s", e)

    def get_audit_log(self, limit: int = 200):
        return self.q("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,))

    # ---- Expenses ----
    def get_expenses(self, vessel_id: int = None, category: str = None, year: int = None):
        sql = "SELECT e.*,v.name vessel_name FROM expenses e LEFT JOIN vessels v ON e.vessel_id=v.id WHERE 1=1"
        params = []
        if vessel_id:
            sql += " AND e.vessel_id=?"
            params.append(vessel_id)
        if category:
            sql += " AND e.category=?"
            params.append(category)
        if year:
            sql += " AND strftime('%Y',e.expense_date)=?"
            params.append(str(year))
        sql += " ORDER BY e.expense_date DESC"
        return self.q(sql, tuple(params))

    def add_expense(self, e: Expense) -> int:
        if not e.category:
            raise ValidationError("Expense category is required")
        if e.amount <= 0:
            raise ValidationError("Amount must be positive")
        if not e.expense_date:
            raise ValidationError("Expense date is required")
        eid = self.ins("""INSERT INTO expenses(vessel_id,trip_id,category,amount,currency,expense_date,description,vendor,receipt_path,tax_deductible,tax_category,gst_rate,gst_amount)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (e.vessel_id if e.vessel_id > 0 else None,
                        e.trip_id, e.category, e.amount, e.currency,
                        e.expense_date, e.description, e.vendor,
                         e.receipt_path, 1 if e.tax_deductible else 0,
                         e.tax_category, e.gst_rate, e.gst_amount))
        self.audit("create", "expense", eid, new_values={"amount": e.amount, "category": e.category})
        return eid

    def update_expense(self, e: Expense) -> int:
        old = self.get_expense(e.id)
        rc = self.u("""UPDATE expenses SET vessel_id=?,trip_id=?,category=?,amount=?,currency=?,expense_date=?,description=?,vendor=?,receipt_path=?,tax_deductible=?,tax_category=?,gst_rate=?,gst_amount=? WHERE id=?""",
                    (e.vessel_id if e.vessel_id > 0 else None, e.trip_id, e.category, e.amount,
                     e.currency, e.expense_date, e.description, e.vendor, e.receipt_path,
                     1 if e.tax_deductible else 0, e.tax_category, e.gst_rate, e.gst_amount, e.id))
        self.audit("update", "expense", e.id, old_values=old, new_values={"amount": e.amount, "category": e.category})
        return rc

    def delete_expense(self, eid: int) -> int:
        old = self.get_expense(eid)
        rc = self.u("DELETE FROM expenses WHERE id=?", (eid,))
        self.audit("delete", "expense", eid, old_values=old)
        return rc

    def get_expense(self, eid: int):
        r = self.q("SELECT e.*,v.name vessel_name FROM expenses e LEFT JOIN vessels v ON e.vessel_id=v.id WHERE e.id=?", (eid,))
        return r[0] if r else None

    def expense_summary(self, vessel_id: int = None, year: int = None):
        sql = "SELECT category,SUM(amount) total,COUNT(*) count,SUM(tax_deductible) deductible_count FROM expenses WHERE 1=1"
        params = []
        if vessel_id:
            sql += " AND vessel_id=?"
            params.append(vessel_id)
        if year:
            sql += " AND strftime('%Y',expense_date)=?"
            params.append(str(year))
        sql += " GROUP BY category ORDER BY total DESC"
        rows = self.q(sql, tuple(params))
        total = sum(r['total'] for r in rows) if rows else 0.0
        return {"by_category": rows, "total": total, "count": sum(r['count'] for r in rows) if rows else 0}

    # ---- Budgets ----
    def get_budgets(self, vessel_id: int = None, year: int = None):
        sql = """SELECT b.*,v.name vessel_name,
                 COALESCE((SELECT SUM(e.amount) FROM expenses e WHERE e.vessel_id=b.vessel_id
                           AND e.category=b.category AND strftime('%Y',e.expense_date)=CAST(b.fiscal_year AS TEXT)),0) spent_amount
                 FROM budgets b LEFT JOIN vessels v ON b.vessel_id=v.id WHERE 1=1"""
        params = []
        if vessel_id:
            sql += " AND b.vessel_id=?"
            params.append(vessel_id)
        if year:
            sql += " AND b.fiscal_year=?"
            params.append(year)
        sql += " ORDER BY b.fiscal_year DESC,b.category"
        return self.q(sql, tuple(params))

    def add_budget(self, b: Budget) -> int:
        if b.budget_amount < 0:
            raise ValidationError("Budget amount cannot be negative")
        bid = self.ins("INSERT INTO budgets(vessel_id,fiscal_year,category,budget_amount,notes) VALUES(?,?,?,?,?)",
                       (b.vessel_id, b.fiscal_year, b.category, b.budget_amount, b.notes))
        self.audit("create", "budget", bid, new_values={"amount": b.budget_amount, "category": b.category})
        return bid

    def update_budget(self, b: Budget) -> int:
        old = self.q("SELECT * FROM budgets WHERE id=?", (b.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE budgets SET vessel_id=?,fiscal_year=?,category=?,budget_amount=?,notes=? WHERE id=?",
                      (b.vessel_id, b.fiscal_year, b.category, b.budget_amount, b.notes, b.id))
        if rc: self.audit("update", "budget", b.id, old_values=old, new_values={"amount": b.budget_amount, "category": b.category})
        return rc

    def delete_budget(self, bid: int) -> int:
        old = self.q("SELECT * FROM budgets WHERE id=?", (bid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM budgets WHERE id=?", (bid,))
        if rc: self.audit("delete", "budget", bid, old_values=old)
        return rc

    # ---- Fuel Logs ----
    def get_fuel_logs(self, vessel_id: int = None):
        sql = """SELECT fl.*,v.name vessel_name,tl.destination trip_destination
                 FROM fuel_logs fl LEFT JOIN vessels v ON fl.vessel_id=v.id
                 LEFT JOIN trip_logs tl ON fl.trip_id=tl.id WHERE 1=1"""
        params = []
        if vessel_id:
            sql += " AND fl.vessel_id=?"
            params.append(vessel_id)
        sql += " ORDER BY fl.created_at DESC"
        return self.q(sql, tuple(params))

    def add_fuel_log(self, fl: FuelLog) -> int:
        if fl.gallons <= 0:
            raise ValidationError("Gallons must be positive")
        total = fl.total_cost or (fl.gallons * (fl.cost_per_gallon or 0.0))
        fid = self.ins("""INSERT INTO fuel_logs(trip_id,vessel_id,gallons,cost_per_gallon,total_cost,fuel_type,vendor,location,engine_hours)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (fl.trip_id, fl.vessel_id, fl.gallons, fl.cost_per_gallon, total,
                        fl.fuel_type, fl.vendor, fl.location, fl.engine_hours))
        self.audit("create", "fuel_log", fid, new_values={"gallons": fl.gallons, "total_cost": total})
        return fid

    def delete_fuel_log(self, fid: int) -> int:
        old = self.q("SELECT * FROM fuel_logs WHERE id=?", (fid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM fuel_logs WHERE id=?", (fid,))
        if rc: self.audit("delete", "fuel_log", fid, old_values=old)
        return rc

    def fuel_efficiency(self, vessel_id: int = None):
        sql = """SELECT fl.vessel_id,v.name vessel_name,
                 SUM(fl.gallons) total_gallons,SUM(fl.total_cost) total_fuel_cost,
                 COALESCE(SUM(tl.distance),0) total_distance,
                 CASE WHEN SUM(fl.gallons)>0 THEN COALESCE(SUM(tl.distance),0)/SUM(fl.gallons) ELSE 0 END nmpg
                 FROM fuel_logs fl LEFT JOIN vessels v ON fl.vessel_id=v.id
                 LEFT JOIN trip_logs tl ON fl.trip_id=tl.id"""
        params = []
        if vessel_id:
            sql += " WHERE fl.vessel_id=?"
            params.append(vessel_id)
        sql += " GROUP BY fl.vessel_id ORDER BY nmpg DESC"
        return self.q(sql, tuple(params))

    # ---- Financial Reports ----
    def financial_summary(self, year: int = None):
        y = year or datetime.datetime.now().year
        expenses = self.expense_summary(year=y)
        budgets = self.get_budgets(year=y)
        fuel = self.fuel_efficiency()
        total_budget = sum(b['budget_amount'] for b in budgets) if budgets else 0
        total_spent = sum(b['spent_amount'] for b in budgets) if budgets else 0
        return {
            "year": y,
            "total_expenses": expenses['total'],
            "expense_count": expenses['count'],
            "by_category": expenses['by_category'],
            "total_budget": total_budget,
            "total_budget_spent": total_spent,
            "budget_utilization_pct": (total_spent / total_budget * 100) if total_budget > 0 else 0,
            "fuel_summary": fuel,
            "total_fuel_cost": sum(f['total_fuel_cost'] or 0 for f in fuel),
        }

    # ---- App Settings ----
    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        r = self.q("SELECT value FROM app_settings WHERE key=?", (key,))
        return r[0]['value'] if r else default

    def set_setting(self, key: str, value: str):
        self.u("INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now'))", (key, value))

    def get_setting(self, key: str, default: str = "") -> str:
        r = self.q("SELECT value FROM app_settings WHERE key=?", (key,))
        return r[0]['value'] if r else default

    def prune_old_data(self):
        audit_days = self.get_setting("audit_retention_days", "365")
        data_days = self.get_setting("data_retention_days", "730")
        try:
            self.u("DELETE FROM audit_log WHERE timestamp<datetime('now','-%s days')" % audit_days)
        except Exception:
            pass
        cutoff = "datetime('now','-%s days')" % data_days
        for table in ['expenses', 'trip_logs', 'fuel_logs']:
            try:
                self.u("DELETE FROM %s WHERE created_at<%s" % (table, cutoff))
            except Exception:
                pass

    # ---- GDPR Data Export (ISO 27001 / GDPR compliance) ----
    def export_all_user_data(self) -> Dict[str, Any]:
        return {
            "exported_at": datetime.datetime.now().isoformat(),
            "vessels": self.q("SELECT * FROM vessels"),
            "expenses": self.q("SELECT * FROM expenses"),
            "trips": self.q("SELECT * FROM trip_logs"),
            "inventory": self.q("SELECT * FROM inventory_items"),
            "tasks": self.q("SELECT * FROM maintenance_tasks"),
            "suppliers": self.q("SELECT * FROM suppliers"),
            "fuel_logs": self.q("SELECT * FROM fuel_logs"),
            "budgets": self.q("SELECT * FROM budgets"),
            "crew_members": self.q("SELECT * FROM crew_members"),
            "certifications": self.q("SELECT * FROM certifications"),
            "training_records": self.q("SELECT * FROM training_records"),
            "watch_schedules": self.q("SELECT * FROM watch_schedules"),
            "task_templates": self.q("SELECT * FROM task_templates"),
            "task_parts": self.q("SELECT * FROM task_parts"),
            "audit_log": self.q("SELECT * FROM audit_log"),
            "settings": self.q("SELECT * FROM app_settings"),
        }

    def delete_all_user_data(self):
        for table in ['watch_schedules', 'training_records', 'certifications', 'crew_members',
                      'fuel_logs', 'expenses', 'budgets', 'stock_transactions',
                      'task_parts', 'task_templates', 'maintenance_tasks', 'trip_logs',
                      'inventory_items', 'suppliers', 'vessels']:
            self.u("DELETE FROM %s" % table)
        self.audit("gdpr_delete", "all", None)

    # ---- Crew ----
    def get_crew_members(self):
        return self.q("SELECT * FROM crew_members ORDER BY last_name,first_name")

    def get_crew_member(self, cid):
        r = self.q("SELECT * FROM crew_members WHERE id=?", (cid,))
        return r[0] if r else None

    def add_crew_member(self, c):
        DataValidator.validate_crew_member(c)
        rid = self.ins("INSERT INTO crew_members(first_name,last_name,email,phone,emergency_contact,emergency_phone,role,date_joined,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (c.first_name.strip(), c.last_name.strip(), c.email.strip(), c.phone.strip(),
             c.emergency_contact.strip(), c.emergency_phone.strip(), c.role.strip(),
             c.date_joined.strip(), c.status, c.notes.strip()))
        self.audit("create", "crew_member", rid, new_values=c.__dict__)
        return rid

    def update_crew_member(self, c):
        DataValidator.validate_crew_member(c)
        old = self.get_crew_member(c.id)
        rc = self.u("UPDATE crew_members SET first_name=?,last_name=?,email=?,phone=?,emergency_contact=?,emergency_phone=?,role=?,date_joined=?,date_left=?,status=?,notes=? WHERE id=?",
            (c.first_name.strip(), c.last_name.strip(), c.email.strip(), c.phone.strip(),
             c.emergency_contact.strip(), c.emergency_phone.strip(), c.role.strip(),
             c.date_joined.strip(), c.date_left.strip(), c.status, c.notes.strip(), c.id))
        if rc: self.audit("update", "crew_member", c.id, old_values=old, new_values=c.__dict__)
        return rc

    def delete_crew_member(self, cid):
        old = self.get_crew_member(cid)
        rc = self.u("DELETE FROM crew_members WHERE id=?", (cid,))
        if rc: self.audit("delete", "crew_member", cid, old_values=old)
        return rc

    def crew_counts(self):
        active = self.q("SELECT COUNT(*) c FROM crew_members WHERE status='active'")
        expiring = self.q("SELECT COUNT(*) c FROM certifications WHERE expiry_date IS NOT NULL AND expiry_date<=date('now','+30 days') AND expiry_date>=date('now')")
        expired_certs = self.q("SELECT COUNT(*) c FROM certifications WHERE expiry_date IS NOT NULL AND expiry_date<date('now')")
        return {'active': active[0]['c'] if active else 0,
                'expiring_certs': expiring[0]['c'] if expiring else 0,
                'expired_certs': expired_certs[0]['c'] if expired_certs else 0}

    # ---- Certifications ----
    def get_certifications(self, crew_id=None):
        if crew_id:
            return self.q("SELECT c.*,cm.first_name||' '||cm.last_name crew_member FROM certifications c LEFT JOIN crew_members cm ON c.crew_member_id=cm.id WHERE c.crew_member_id=? ORDER BY c.expiry_date", (crew_id,))
        return self.q("SELECT c.*,cm.first_name||' '||cm.last_name crew_member FROM certifications c LEFT JOIN crew_members cm ON c.crew_member_id=cm.id ORDER BY c.expiry_date")

    def add_certification(self, c):
        DataValidator.validate_certification(c)
        rid=self.ins("INSERT INTO certifications(crew_member_id,name,issuing_body,cert_number,category,issue_date,expiry_date) VALUES(?,?,?,?,?,?,?)",
            (c.crew_member_id, c.name.strip(), c.issuing_body.strip(), c.cert_number.strip(),
             c.category.strip(), c.issue_date.strip(), c.expiry_date.strip()))
        self.audit("create", "certification", rid, new_values=c.__dict__)
        return rid

    def update_certification(self, c):
        DataValidator.validate_certification(c)
        old=self.q("SELECT * FROM certifications WHERE id=?",(c.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE certifications SET crew_member_id=?,name=?,issuing_body=?,cert_number=?,category=?,issue_date=?,expiry_date=? WHERE id=?",
            (c.crew_member_id, c.name.strip(), c.issuing_body.strip(), c.cert_number.strip(),
             c.category.strip(), c.issue_date.strip(), c.expiry_date.strip(), c.id))
        if rc: self.audit("update", "certification", c.id, old_values=old, new_values=c.__dict__)
        return rc

    def delete_certification(self, cid):
        old=self.q("SELECT * FROM certifications WHERE id=?",(cid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM certifications WHERE id=?", (cid,))
        if rc: self.audit("delete", "certification", cid, old_values=old)
        return rc

    # ---- Training Records ----
    def get_training_records(self, crew_id=None):
        if crew_id:
            return self.q("SELECT t.*,cm.first_name||' '||cm.last_name crew_member FROM training_records t LEFT JOIN crew_members cm ON t.crew_member_id=cm.id WHERE t.crew_member_id=? ORDER BY t.date_completed DESC", (crew_id,))
        return self.q("SELECT t.*,cm.first_name||' '||cm.last_name crew_member FROM training_records t LEFT JOIN crew_members cm ON t.crew_member_id=cm.id ORDER BY t.date_completed DESC")

    def add_training_record(self, t):
        DataValidator.validate_training(t)
        rid=self.ins("INSERT INTO training_records(crew_member_id,course_name,provider,date_completed,expiry_date,cost,notes) VALUES(?,?,?,?,?,?,?)",
            (t.crew_member_id, t.course_name.strip(), t.provider.strip(), t.date_completed.strip(),
             t.expiry_date.strip(), t.cost, t.notes.strip()))
        self.audit("create", "training_record", rid, new_values=t.__dict__)
        return rid

    def update_training_record(self, t):
        DataValidator.validate_training(t)
        old=self.q("SELECT * FROM training_records WHERE id=?",(t.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE training_records SET crew_member_id=?,course_name=?,provider=?,date_completed=?,expiry_date=?,cost=?,notes=? WHERE id=?",
            (t.crew_member_id, t.course_name.strip(), t.provider.strip(), t.date_completed.strip(),
             t.expiry_date.strip(), t.cost, t.notes.strip(), t.id))
        if rc: self.audit("update", "training_record", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_training_record(self, tid):
        old=self.q("SELECT * FROM training_records WHERE id=?",(tid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM training_records WHERE id=?", (tid,))
        if rc: self.audit("delete", "training_record", tid, old_values=old)
        return rc

    # ---- Watch Schedules ----
    def get_watch_schedules(self, vessel_id=None, watch_date=None):
        q = "SELECT ws.*,cm.first_name||' '||cm.last_name crew_name,v.name vessel_name FROM watch_schedules ws LEFT JOIN crew_members cm ON ws.crew_member_id=cm.id LEFT JOIN vessels v ON ws.vessel_id=v.id"
        params = []
        where = []
        if vessel_id:
            where.append("ws.vessel_id=?"); params.append(vessel_id)
        if watch_date:
            where.append("ws.date=?"); params.append(watch_date)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ws.date,ws.start_time"
        return self.q(q, tuple(params))

    def add_watch_schedule(self, w):
        DataValidator.validate_watch_schedule(w)
        rid=self.ins("INSERT INTO watch_schedules(crew_member_id,vessel_id,date,start_time,end_time,role_on_watch,notes) VALUES(?,?,?,?,?,?,?)",
            (w.crew_member_id, w.vessel_id, w.date.strip(), w.start_time.strip(), w.end_time.strip(), w.role_on_watch.strip(), w.notes.strip()))
        self.audit("create", "watch_schedule", rid, new_values=w.__dict__)
        return rid

    def update_watch_schedule(self, w):
        DataValidator.validate_watch_schedule(w)
        old=self.q("SELECT * FROM watch_schedules WHERE id=?",(w.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE watch_schedules SET crew_member_id=?,vessel_id=?,date=?,start_time=?,end_time=?,role_on_watch=?,notes=? WHERE id=?",
            (w.crew_member_id, w.vessel_id, w.date.strip(), w.start_time.strip(), w.end_time.strip(), w.role_on_watch.strip(), w.notes.strip(), w.id))
        if rc: self.audit("update", "watch_schedule", w.id, old_values=old, new_values=w.__dict__)
        return rc

    def delete_watch_schedule(self, wid):
        old=self.q("SELECT * FROM watch_schedules WHERE id=?",(wid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM watch_schedules WHERE id=?", (wid,))
        if rc: self.audit("delete", "watch_schedule", wid, old_values=old)
        return rc

    # ---- Task Parts ----
    def get_task_parts(self, task_id):
        return self.q("SELECT tp.*,ii.name part_name FROM task_parts tp LEFT JOIN inventory_items ii ON tp.item_id=ii.id WHERE tp.task_id=? ORDER BY tp.id", (task_id,))

    def add_task_part(self, tp):
        DataValidator.validate_task_part(tp)
        rid=self.ins("INSERT INTO task_parts(task_id,item_id,quantity_used,cost_per_unit) VALUES(?,?,?,?)",
            (tp.task_id, tp.item_id, tp.quantity_used, tp.cost_per_unit))
        self.audit("create", "task_part", rid, new_values=tp.__dict__)
        return rid

    def delete_task_part(self, tpid):
        old=self.q("SELECT * FROM task_parts WHERE id=?",(tpid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM task_parts WHERE id=?", (tpid,))
        if rc: self.audit("delete", "task_part", tpid, old_values=old)
        return rc

    # ---- Task Templates ----
    def get_task_templates(self):
        return self.q("SELECT * FROM task_templates ORDER BY name")

    def add_task_template(self, t):
        DataValidator.validate_template(t)
        rid=self.ins("INSERT INTO task_templates(name,system,description,priority,interval_days,interval_hours,default_vessel_type) VALUES(?,?,?,?,?,?,?)",
            (t.name.strip(), t.system.strip(), t.description.strip(), t.priority, t.interval_days, t.interval_hours, t.default_vessel_type.strip()))
        self.audit("create", "task_template", rid, new_values=t.__dict__)
        return rid

    def update_task_template(self, t):
        DataValidator.validate_template(t)
        old=self.q("SELECT * FROM task_templates WHERE id=?",(t.id,))
        old=old[0] if old else None
        rc=self.u("UPDATE task_templates SET name=?,system=?,description=?,priority=?,interval_days=?,interval_hours=?,default_vessel_type=? WHERE id=?",
            (t.name.strip(), t.system.strip(), t.description.strip(), t.priority, t.interval_days, t.interval_hours, t.default_vessel_type.strip(), t.id))
        if rc: self.audit("update", "task_template", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_task_template(self, tid):
        old=self.q("SELECT * FROM task_templates WHERE id=?",(tid,))
        old=old[0] if old else None
        rc=self.u("DELETE FROM task_templates WHERE id=?", (tid,))
        if rc: self.audit("delete", "task_template", tid, old_values=old)
        return rc

    # ---- Workflow ----
    def next_work_order(self):
        r = self.q("SELECT MAX(CAST(SUBSTR(work_order,4) AS INTEGER)) mx FROM maintenance_tasks WHERE work_order LIKE 'WO-%'")
        nxt = (r[0]['mx'] or 0) + 1
        return "WO-%05d" % nxt

    def complete_task(self, task_id, completed_by, completed_notes, cost):
        with self.connection_pool.get_connection() as conn:
            task = conn.execute("SELECT * FROM maintenance_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValidationError("Task not found")
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE maintenance_tasks SET status='completed',completed_at=?,completed_by=?,completed_notes=?,cost=? WHERE id=?",
                (now, completed_by, completed_notes, cost, task_id))
            # Deduct parts from inventory
            parts = conn.execute("SELECT tp.*,ii.current_stock FROM task_parts tp JOIN inventory_items ii ON tp.item_id=ii.id WHERE tp.task_id=?", (task_id,)).fetchall()
            for p in parts:
                new_stock = max(0, p['current_stock'] - p['quantity_used'])
                conn.execute("UPDATE inventory_items SET current_stock=? WHERE id=?", (new_stock, p['item_id']))
            # Auto-create recurring task if applicable
            if task['is_recurring'] and task['next_due'] and task['interval_days']:
                from datetime import datetime as dt, timedelta
                try:
                    old_due = dt.strptime(task['next_due'], "%Y-%m-%d")
                    new_due = (old_due + timedelta(days=task['interval_days'])).strftime("%Y-%m-%d")
                    wo = self.next_work_order()
                    conn.execute("""INSERT INTO maintenance_tasks(vessel_id,system,task_name,description,next_due,status,priority,interval_days,interval_hours,is_recurring,work_order)
                        VALUES(?,?,?,?,?,'pending',?,?,?,1,?)""",
                        (task['vessel_id'], task['system'], task['task_name'], task['description'],
                         new_due, task['priority'], task['interval_days'], task['interval_hours'], wo))
                except Exception:
                    pass
            conn.commit()
        self.audit("complete_task", "maintenance_task", task_id,
            new_values={"status":"completed","completed_by":completed_by,"completed_notes":completed_notes,"cost":cost})

    def check_overdue_tasks(self):
        count = self.u("UPDATE maintenance_tasks SET status='overdue' WHERE status='pending' AND next_due IS NOT NULL AND next_due<date('now')")
        if count:
            logger.info("Auto-overdue: %d tasks marked overdue", count)
        return count

    # ---- Engine Hours ----
    def get_vessel_engine_hours(self, vessel_id):
        r = self.q("SELECT engine_hours,last_hours_update FROM vessels WHERE id=?", (vessel_id,))
        return r[0] if r else None

    def update_vessel_engine_hours(self, vessel_id, hours):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.u("UPDATE vessels SET engine_hours=?,last_hours_update=? WHERE id=?", (hours, now, vessel_id))

    # ---- Export ----
    def export_csv(self, path: str, query: str, params: tuple = ()) -> int:
        p = Path(path)
        parent = p.parent
        if not parent.exists():
            raise ValidationError("Directory does not exist: %s" % parent)
        if not p.parent.is_dir():
            raise ValidationError("Not a directory: %s" % parent)
        rows = self.q(query, params)
        if not rows:
            return 0
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return len(rows)

    def close(self): self.connection_pool.close_all()

# =============================================================================
# DIALOGS
# =============================================================================
class EntityDialog(Gtk.Dialog):
    def __init__(self,parent,title,fields):
        super().__init__(title=title,transient_for=parent,flags=0,modal=True)
        self.set_default_size(480,-1)
        self.add_button("Cancel",Gtk.ResponseType.CANCEL); self.add_button("Save",Gtk.ResponseType.OK)
        box=self.get_content_area(); box.set_spacing(6); box.set_border_width(12)
        self._entries={}
        for key,label,typ in fields:
            row=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=6)
            lbl=Gtk.Label(label=label,xalign=0); lbl.set_size_request(130,-1)
            row.pack_start(lbl,False,False,0)
            if typ==bool:
                w=Gtk.ComboBoxText(); w.append("0","No"); w.append("1","Yes"); w.set_active(0)
            elif typ=='combo':
                w=Gtk.ComboBoxText(); w.set_entry_text_column(0)
            elif typ=='textview':
                sw=Gtk.ScrolledWindow(); sw.set_size_request(-1,80)
                tv=Gtk.TextView(); tv.set_wrap_mode(Gtk.WrapMode.WORD); sw.add(tv)
                self._entries[f"{key}_tv"]=tv; w=sw
            else:
                w=Gtk.Entry()
            row.pack_start(w,True,True,0); box.pack_start(row,False,False,0)
            self._entries[key]=w
        self.show_all()
    def get_val(self,key):
        w=self._entries[key]
        if isinstance(w,Gtk.Entry): return w.get_text()
        if isinstance(w,Gtk.ComboBoxText): return w.get_active_text() or ""
        if isinstance(w,Gtk.ScrolledWindow):
            tv=self._entries.get(f"{key}_tv")
            if tv: b=tv.get_buffer(); return b.get_text(b.get_start_iter(),b.get_end_iter(),True) or ""
        return ""
    def set_val(self,key,text):
        w=self._entries.get(key)
        if isinstance(w,Gtk.Entry): w.set_text(str(text or ""))
        elif isinstance(w,Gtk.ScrolledWindow):
            tv=self._entries.get(f"{key}_tv")
            if tv: tv.get_buffer().set_text(str(text or ""))
    def set_combo(self,key,items,active=None):
        w=self._entries.get(key)
        if isinstance(w,Gtk.ComboBoxText):
            w.remove_all()
            for it in items: w.append(it,it)
            if active and active in items: w.set_active(items.index(active))
            elif items: w.set_active(0)

class VesselDialog(EntityDialog):
    def __init__(self,parent,vessel=None):
        super().__init__(parent,"Edit Vessel" if vessel and vessel.id else "Add Vessel",
            [("name","Vessel Name *",str),("vessel_type","Vessel Type",str),("engine_make","Engine Make",str),("engine_model","Engine Model",str),("current_location","Current Location",str)])
        self._vid=vessel.id if vessel else None
        if vessel:
            self.set_val("name",vessel.name); self.set_val("vessel_type",vessel.vessel_type)
            self.set_val("engine_make",vessel.engine_make); self.set_val("engine_model",vessel.engine_model)
            self.set_val("current_location",vessel.current_location)
    def get_vessel(self):
        return Vessel(id=self._vid,name=self.get_val("name"),vessel_type=self.get_val("vessel_type"),
            engine_make=self.get_val("engine_make"),engine_model=self.get_val("engine_model"),current_location=self.get_val("current_location"))

class MaintenanceDialog(EntityDialog):
    def __init__(self,parent,db,task=None):
        super().__init__(parent,"Edit Task" if task and task.id else "Add Maintenance Task",
            [("vessel_id","Vessel *",'combo'),("system","System",str),("task_name","Task Name *",str),
             ("description","Description",'textview'),("next_due","Due (YYYY-MM-DD)",str),
             ("status","Status",'combo'),("priority","Priority",'combo'),
             ("interval_days","Interval (days)",str),("interval_hours","Interval (hours)",str),
             ("is_recurring","Recurring",bool),("cost","Cost",str)])
        vessels=db.get_vessels(); self._vmap={v['name']:v['id'] for v in vessels}; self._rvmap={v['id']:v['name'] for v in vessels}
        self.set_combo("vessel_id",list(self._vmap.keys())); self.set_combo("status",["pending","in_progress","completed","overdue"]); self.set_combo("priority",["low","medium","high","critical"])
        self._tid=task.id if task else None
        if task:
            self.set_combo("vessel_id",list(self._vmap.keys()),self._rvmap.get(task.vessel_id,''))
            self.set_val("system",task.system); self.set_val("task_name",task.task_name); self.set_val("description",task.description)
            self.set_val("next_due",task.next_due)
            self.set_combo("status",["pending","in_progress","completed","overdue"],task.status)
            self.set_combo("priority",["low","medium","high","critical"],task.priority)
            self.set_val("interval_days",str(task.interval_days or ''))
            self.set_val("interval_hours",str(task.interval_hours or ''))
            self.set_combo("is_recurring",["0","1"],"1" if task.is_recurring else "0")
            self.set_val("cost",str(task.cost or ''))
    def get_task(self):
        vn=self.get_val("vessel_id")
        try: int_days = int(self.get_val("interval_days")) if self.get_val("interval_days") else None
        except ValueError: int_days = None
        try: int_hrs = float(self.get_val("interval_hours")) if self.get_val("interval_hours") else None
        except ValueError: int_hrs = None
        try: cst = float(self.get_val("cost") or 0)
        except ValueError: cst = 0.0
        return MaintenanceTask(id=self._tid, vessel_id=self._vmap.get(vn,0), vessel_name=vn,
            system=self.get_val("system"), task_name=self.get_val("task_name"),
            description=self.get_val("description"), next_due=self.get_val("next_due"),
            status=self.get_val("status"), priority=self.get_val("priority"),
            interval_days=int_days, interval_hours=int_hrs,
            is_recurring=self.get_val("is_recurring")=="1", cost=cst)

class CompleteTaskDialog(Gtk.Dialog):
    def __init__(self, parent, task_id, task_name):
        super().__init__(title="Complete Task", transient_for=parent, flags=0, modal=True)
        self.set_default_size(400, 250)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Complete", Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Complete: %s" % task_name), False, False, 0)
        rows = [
            ("completed_by", "Completed By:", Gtk.Entry()),
            ("notes", "Notes:", Gtk.Entry()),
            ("cost", "Cost:", Gtk.Entry()),
        ]
        self._entries = {}
        for key, label, w in rows:
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            r.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            w.set_width_chars(25)
            r.pack_start(w, True, True, 0)
            box.pack_start(r, False, False, 0)
            self._entries[key] = w
        self.show_all()

    def get_data(self):
        return (self._entries['completed_by'].get_text(), self._entries['notes'].get_text(),
                float(self._entries['cost'].get_text() or 0))

class TaskPartsDialog(Gtk.Dialog):
    def __init__(self, parent, db, task_id, task_name):
        super().__init__(title="Task Parts - %s" % task_name, transient_for=parent, flags=0, modal=True)
        self.set_default_size(600, 350)
        self._db = db
        self._task_id = task_id
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        hdr = Gtk.Label()
        hdr.set_markup("<b>Parts used by: %s</b>" % task_name)
        box.pack_start(hdr, False, False, 0)
        tb = Gtk.Toolbar()
        add_btn = Gtk.ToolButton(label="Add Part", icon_name="list-add-symbolic")
        add_btn.connect("clicked", lambda x: self._add_part())
        tb.insert(add_btn, -1)
        del_btn = Gtk.ToolButton(label="Delete", icon_name="edit-delete-symbolic")
        del_btn.connect("clicked", lambda x: self._del_part())
        tb.insert(del_btn, -1)
        box.pack_start(tb, False, False, 0)
        self._part_store = Gtk.ListStore(int, str, int, float)
        tv = Gtk.TreeView(model=self._part_store)
        for i, n in [(1, "Part Name"), (2, "Qty Used"), (3, "Cost/Unit")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        self._part_tv = tv
        self._load_parts()
        self.show_all()

    def _load_parts(self):
        self._part_store.clear()
        for p in self._db.get_task_parts(self._task_id):
            self._part_store.append([p['id'], p.get('part_name', ''), p['quantity_used'], p['cost_per_unit']])

    def _add_part(self):
        items = self._db.get_items()
        if not items:
            show_error(self, "No inventory items available")
            return
        dlg = Gtk.Dialog(title="Add Part", transient_for=self, flags=0, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Add", Gtk.ResponseType.OK)
        bx = dlg.get_content_area()
        bx.set_spacing(6)
        bx.set_border_width(12)
        bx.pack_start(Gtk.Label(label="Item:"), False, False, 0)
        combo = Gtk.ComboBoxText()
        item_map = {}
        for it in items:
            label = "%s (stock: %d)" % (it['name'], it['current_stock'])
            combo.append(label, label)
            item_map[label] = it
        combo.set_active(0)
        bx.pack_start(combo, False, False, 0)
        bx.pack_start(Gtk.Label(label="Quantity:"), False, False, 0)
        qty_entry = Gtk.Entry()
        qty_entry.set_text("1")
        bx.pack_start(qty_entry, False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            sel = combo.get_active_text()
            if sel and sel in item_map:
                item = item_map[sel]
                try:
                    qty = int(qty_entry.get_text() or 1)
                    tp = TaskPart(task_id=self._task_id, item_id=item['id'], quantity_used=qty, cost_per_unit=item['unit_cost'])
                    self._db.add_task_part(tp)
                    self._load_parts()
                except (ValidationError, DatabaseError, ValueError) as e:
                    show_error(self, str(e))
        dlg.destroy()

    def _del_part(self):
        sel = self._part_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        pid = self._part_store.get_value(ti, 0)
        if confirm(self, "Remove this part from the task?"):
            try:
                self._db.delete_task_part(pid)
                self._load_parts()
            except DatabaseError as e:
                show_error(self, str(e))

class InventoryDialog(EntityDialog):
    def __init__(self,parent,item=None):
        super().__init__(parent,"Edit Item" if item else "Add Inventory Item",
            [("name","Item Name *",str),("part_number","Part Number",str),("category","Category",str),("current_stock","Current Stock",str),("minimum_stock","Minimum Stock",str),("unit_cost","Unit Cost",str)])
        self._iid=item['id'] if item else None
        if item:
            for k in ('name','part_number','category'): self.set_val(k,item.get(k,''))
            self.set_val("current_stock",str(item.get('current_stock',0))); self.set_val("minimum_stock",str(item.get('minimum_stock',0))); self.set_val("unit_cost",str(item.get('unit_cost',0.0)))
    def get_data(self):
        return {'id':self._iid,'name':self.get_val("name"),'part_number':self.get_val("part_number"),'category':self.get_val("category"),
            'current_stock':int(self.get_val("current_stock") or 0),'minimum_stock':int(self.get_val("minimum_stock") or 0),'unit_cost':float(self.get_val("unit_cost") or 0.0)}

class StockAdjustDialog(Gtk.Dialog):
    def __init__(self,parent,item_name,current_stock):
        super().__init__(title=f"Adjust Stock: {item_name}",transient_for=parent,flags=0,modal=True)
        self.set_default_size(350,-1); self.add_button("Cancel",Gtk.ResponseType.CANCEL); self.add_button("Apply",Gtk.ResponseType.OK)
        box=self.get_content_area(); box.set_spacing(8); box.set_border_width(12)
        box.pack_start(Gtk.Label(label=f"Current stock: {current_stock}"),False,False,0)
        row=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=6)
        row.pack_start(Gtk.Label(label="Change (+/-):"),False,False,0)
        self._entry=Gtk.Entry(); self._entry.set_width_chars(10); row.pack_start(self._entry,False,False,0)
        box.pack_start(row,False,False,0)
        self._note_entry=Gtk.Entry(); self._note_entry.set_placeholder_text("Reason (optional)")
        box.pack_start(self._note_entry,False,False,0)
        h=Gtk.Label(label="Use positive to add stock, negative to remove"); h.set_xalign(0)
        h.get_style_context().add_class("dim-label"); box.pack_start(h,False,False,0)
        self.show_all()
    def get_delta(self):
        try: return int(self._entry.get_text())
        except ValueError: raise ValidationError("Enter a valid integer (+/-)")
    def get_note(self): return self._note_entry.get_text()

class ExpenseDialog(EntityDialog):
    CATEGORIES = ["Fuel", "Maintenance", "Supplies", "Dockage", "Crew", "Insurance", "Registration", "Equipment", "Repairs", "Food", "Utilities", "Miscellaneous"]
    TAX_CATEGORIES = ["", "GST", "VAT", "Sales Tax", "Excise", "Luxury Tax"]

    def __init__(self, parent, db, expense=None):
        super().__init__(parent, "Edit Expense" if expense and expense.id else "Add Expense",
            [("vessel_id", "Vessel *", "combo"), ("category", "Category *", "combo"),
             ("amount", "Amount *", str), ("currency", "Currency", "combo"),
             ("expense_date", "Date (YYYY-MM-DD) *", str),
             ("description", "Description", "textview"), ("vendor", "Vendor", str),
             ("tax_deductible", "Tax Deductible", bool), ("tax_category", "Tax Type", "combo")])
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self.set_combo("category", self.CATEGORIES)
        self.set_combo("currency", ["USD", "EUR", "GBP", "CAD", "AUD", "JPY"])
        self.set_combo("tax_category", self.TAX_CATEGORIES)
        # Trip selection
        self._trip_combo = Gtk.ComboBoxText()
        self._trip_combo.append("0", "None")
        trips = db.get_trips()[:50]
        self._trip_map = {}
        for t in trips:
            lbl = "%s - %s" % (t.get('destination', '?'), t.get('departure', ''))
            self._trip_combo.append(str(t['id']), lbl)
            self._trip_map[t['id']] = lbl
        self._trip_combo.set_active(0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(Gtk.Label(label="Trip:", xalign=0), False, False, 0)
        row.pack_start(self._trip_combo, True, True, 0)
        self.get_content_area().pack_start(row, False, False, 0)

        self._eid = expense.id if expense else None
        if expense:
            self.set_combo("vessel_id", list(self._vmap.keys()), self._rvmap.get(expense.vessel_id, ''))
            self.set_combo("category", self.CATEGORIES, expense.category)
            self.set_val("amount", str(expense.amount))
            self.set_val("expense_date", expense.expense_date)
            self.set_val("description", expense.description)
            self.set_val("vendor", expense.vendor)
            self.set_combo("tax_deductible", ["0", "1"], "1" if expense.tax_deductible else "0")
            self.set_combo("tax_category", self.TAX_CATEGORIES, expense.tax_category or "")
            if expense.trip_id:
                self._trip_combo.set_active_id(str(expense.trip_id))

        self.show_all()

    def get_expense(self):
        vn = self.get_val("vessel_id")
        trip_id = int(self._trip_combo.get_active_id() or 0)
        return Expense(
            id=self._eid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
            trip_id=trip_id if trip_id > 0 else None,
            category=self.get_val("category"), amount=float(self.get_val("amount") or 0),
            currency=self.get_val("currency") or "USD",
            expense_date=self.get_val("expense_date"), description=self.get_val("description"),
            vendor=self.get_val("vendor"), tax_deductible=self.get_val("tax_deductible") == "1",
            tax_category=self.get_val("tax_category"))

class TripDialog(EntityDialog):
    def __init__(self,parent,db,trip=None):
        super().__init__(parent,"Edit Trip" if trip and trip.id else "Add Trip Log",
            [("vessel_id","Vessel *",'combo'),("departure","Departure",str),("arrival","Arrival",str),("destination","Destination",str),("distance","Distance (nm)",str),("fuel_used","Fuel Used",str)])
        vessels=db.get_vessels(); self._vmap={v['name']:v['id'] for v in vessels}; self._rvmap={v['id']:v['name'] for v in vessels}
        self.set_combo("vessel_id",list(self._vmap.keys()))
        self._tid=trip.id if trip else None
        if trip:
            self.set_combo("vessel_id",list(self._vmap.keys()),self._rvmap.get(trip.vessel_id,''))
            self.set_val("departure",trip.departure); self.set_val("arrival",trip.arrival); self.set_val("destination",trip.destination)
            self.set_val("distance",str(trip.distance)); self.set_val("fuel_used",str(trip.fuel_used))
    def get_trip(self):
        vn=self.get_val("vessel_id")
        return TripLog(id=self._tid,vessel_id=self._vmap.get(vn,0),vessel_name=vn,departure=self.get_val("departure"),
            arrival=self.get_val("arrival"),destination=self.get_val("destination"),
            distance=float(self.get_val("distance") or 0),fuel_used=float(self.get_val("fuel_used") or 0))

class SupplierDialog(EntityDialog):
    def __init__(self,parent,supplier=None):
        super().__init__(parent,"Edit Supplier" if supplier and supplier.id else "Add Supplier",
            [("name","Supplier Name *",str),("contact","Contact Person",str),("phone","Phone",str),("email","Email",str),("preferred","Preferred",bool)])
        self._sid=supplier.id if supplier else None
        if supplier:
            self.set_val("name",supplier.name); self.set_val("contact",supplier.contact)
            self.set_val("phone",supplier.phone); self.set_val("email",supplier.email)
            self.set_combo("preferred",["0","1"],"1" if supplier.preferred else "0")
    def get_supplier(self):
        return Supplier(id=self._sid,name=self.get_val("name"),contact=self.get_val("contact"),phone=self.get_val("phone"),email=self.get_val("email"),preferred=self.get_val("preferred")=="1")

class CrewDialog(EntityDialog):
    ROLES = ["Captain", "First Mate", "Engineer", "Deckhand", "Cook", "Crew", "Purser", "Engine Cadet", "Deck Cadet", "Electrician", "Bosun"]
    STATUSES = ["active", "inactive", "terminated"]

    def __init__(self, parent, db, crew_member=None):
        super().__init__(parent, "Edit Crew Member" if crew_member and crew_member.id else "Add Crew Member",
            [("first_name", "First Name *", str), ("last_name", "Last Name *", str),
             ("role", "Role *", "combo"), ("status", "Status", "combo"),
             ("email", "Email", str), ("phone", "Phone", str),
             ("emergency_contact", "Emergency Contact", str), ("emergency_phone", "Emergency Phone", str),
             ("date_joined", "Date Joined", str)])
        self.set_combo("role", self.ROLES)
        self.set_combo("status", self.STATUSES)
        self._cid = crew_member.id if crew_member else None
        if crew_member:
            self.set_val("first_name", crew_member.first_name)
            self.set_val("last_name", crew_member.last_name)
            self.set_combo("role", self.ROLES, crew_member.role)
            self.set_combo("status", self.STATUSES, crew_member.status)
            self.set_val("email", crew_member.email)
            self.set_val("phone", crew_member.phone)
            self.set_val("emergency_contact", crew_member.emergency_contact)
            self.set_val("emergency_phone", crew_member.emergency_phone)
            self.set_val("date_joined", crew_member.date_joined)

    def get_crew_member(self):
        return CrewMember(id=self._cid, first_name=self.get_val("first_name"), last_name=self.get_val("last_name"),
                          role=self.get_val("role"), status=self.get_val("status"),
                          email=self.get_val("email"), phone=self.get_val("phone"),
                          emergency_contact=self.get_val("emergency_contact"),
                          emergency_phone=self.get_val("emergency_phone"),
                          date_joined=self.get_val("date_joined"))

class TemplateDialog(EntityDialog):
    PRIORITIES = ["low", "medium", "high", "critical"]

    def __init__(self, parent, db, template=None):
        super().__init__(parent, "Edit Template" if template and template.id else "Add Task Template",
            [("name", "Template Name *", str), ("system", "System", str),
             ("description", "Description", "textview"), ("priority", "Priority", "combo"),
             ("interval_days", "Interval (days)", str), ("interval_hours", "Interval (hours)", str),
             ("default_vessel_type", "Default Vessel Type", str)])
        self.set_combo("priority", self.PRIORITIES)
        self._tid = template.id if template else None
        if template:
            self.set_val("name", template.name)
            self.set_val("system", template.system)
            self.set_val("description", template.description)
            self.set_combo("priority", self.PRIORITIES, template.priority)
            self.set_val("interval_days", str(template.interval_days or ''))
            self.set_val("interval_hours", str(template.interval_hours or ''))
            self.set_val("default_vessel_type", template.default_vessel_type)
    def get_template(self):
        try: int_days = int(self.get_val("interval_days")) if self.get_val("interval_days") else None
        except ValueError: int_days = None
        try: int_hrs = float(self.get_val("interval_hours")) if self.get_val("interval_hours") else None
        except ValueError: int_hrs = None
        return TaskTemplate(id=self._tid, name=self.get_val("name"), system=self.get_val("system"),
            description=self.get_val("description"), priority=self.get_val("priority"),
            interval_days=int_days, interval_hours=int_hrs,
            default_vessel_type=self.get_val("default_vessel_type"))

class CertDialog(Gtk.Dialog):
    def __init__(self, parent, db, crew_id, crew_name):
        super().__init__(title="Certifications - %s" % crew_name, transient_for=parent, flags=0, modal=True)
        self.set_default_size(700, 450)
        self._db = db
        self._crew_id = crew_id
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)

        hdr = Gtk.Label()
        hdr.set_markup("<b>Certifications for %s</b>" % crew_name)
        box.pack_start(hdr, False, False, 0)

        tb = Gtk.Toolbar()
        add_btn = Gtk.ToolButton(label="Add Cert", icon_name="list-add-symbolic")
        add_btn.connect("clicked", lambda x: self._add_cert())
        tb.insert(add_btn, -1)
        del_btn = Gtk.ToolButton(label="Delete", icon_name="edit-delete-symbolic")
        del_btn.connect("clicked", lambda x: self._del_cert())
        tb.insert(del_btn, -1)
        box.pack_start(tb, False, False, 0)

        self._cert_store = Gtk.ListStore(int, str, str, str, str, str)
        tv = Gtk.TreeView(model=self._cert_store)
        for i, n in [(1, "Certification"), (2, "Issuing Body"), (3, "Number"), (4, "Issue Date"), (5, "Expiry Date")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        self._cert_tv = tv
        self._load_certs()
        self.show_all()

    def _load_certs(self):
        self._cert_store.clear()
        for cert in self._db.get_certifications(self._crew_id):
            self._cert_store.append([
                cert['id'], cert.get('name', ''), cert.get('issuing_body', ''),
                cert.get('cert_number', ''), cert.get('issue_date', ''),
                cert.get('expiry_date', '')
            ])

    def _add_cert(self):
        d = CertEntryDialog(self, self._db, self._crew_id)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
            self._load_certs()
        d.destroy()

    def _del_cert(self):
        sel = self._cert_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        cid = self._cert_store.get_value(ti, 0)
        name = self._cert_store.get_value(ti, 1)
        if confirm(self, "Delete cert '%s'?" % name):
            try:
                self._db.delete_certification(cid)
                self._load_certs()
            except DatabaseError as e:
                show_error(self, str(e))

class CertEntryDialog(EntityDialog):
    CATEGORIES = ["STCW", "Safety", "Engineering", "Medical", "Navigation", "Security", "GMDSS", "Other"]

    def __init__(self, parent, db, crew_id):
        super().__init__(parent, "Add Certification",
            [("name", "Certification Name *", str), ("issuing_body", "Issuing Body", str),
             ("cert_number", "Certificate Number", str), ("category", "Category", "combo"),
             ("issue_date", "Issue Date (YYYY-MM-DD)", str), ("expiry_date", "Expiry Date (YYYY-MM-DD)", str)])
        self._db = db
        self._crew_id = crew_id
        self.set_combo("category", self.CATEGORIES)

    def apply(self):
        c = Certification(crew_member_id=self._crew_id, name=self.get_val("name"),
                          issuing_body=self.get_val("issuing_body"), cert_number=self.get_val("cert_number"),
                          category=self.get_val("category"), issue_date=self.get_val("issue_date"),
                          expiry_date=self.get_val("expiry_date"))
        try:
            self._db.add_certification(c)
        except (ValidationError, DatabaseError) as e:
            show_error(self, str(e))
            raise
        return c

class TrainingDialog(EntityDialog):
    COURSES = ["STCW Basic Safety", "Fire Fighting", "First Aid", "Survival Craft", "Radar Observer",
               "GMDSS", "Advanced Fire Fighting", "Medical Care", "Bridge Resource Management",
               "Engine Room Resource Management", "ECDIS", "Ship Security"]
    def __init__(self, parent, db, record=None):
        super().__init__(parent, "Edit Training Record" if record and record.id else "Add Training Record",
            [("crew_member_id", "Crew Member *", "combo"), ("course_name", "Course Name *", "combo"),
             ("provider", "Provider", str), ("date_completed", "Date Completed", str),
             ("expiry_date", "Expiry Date", str), ("cost", "Cost", str), ("notes", "Notes", "textview")])
        self._db = db
        crew = db.get_crew_members()
        self._cmap = {("%s %s" % (c['first_name'], c['last_name'])): c['id'] for c in crew}
        self._rcmap = {v: k for k, v in self._cmap.items()}
        self.set_combo("crew_member_id", list(self._cmap.keys()))
        self.set_combo("course_name", self.COURSES)
        self._tid = record.id if record else None
        if record:
            name = self._rcmap.get(record.crew_member_id, "")
            self.set_combo("crew_member_id", list(self._cmap.keys()), name)
            self.set_combo("course_name", self.COURSES, record.course_name)
            self.set_val("provider", record.provider)
            self.set_val("date_completed", record.date_completed)
            self.set_val("expiry_date", record.expiry_date)
            self.set_val("cost", str(record.cost or ""))
            self.set_val("notes", record.notes)
    def get_training_record(self):
        cname = self.get_val("crew_member_id")
        cid = self._cmap.get(cname, 0)
        try: cost = float(self.get_val("cost") or 0)
        except ValueError: cost = 0.0
        return TrainingRecord(id=self._tid, crew_member_id=cid, course_name=self.get_val("course_name"),
            provider=self.get_val("provider"), date_completed=self.get_val("date_completed"),
            expiry_date=self.get_val("expiry_date"), cost=cost, notes=self.get_val("notes"))

class WatchScheduleDialog(EntityDialog):
    ROLES = ["OOW", "Chief Officer", "Second Officer", "Third Officer", "Lookout", "Helmsman",
             "Engineering Watch", "Electrician Watch", "Deck Watch", "Safety Officer"]
    def __init__(self, parent, db, schedule=None):
        super().__init__(parent, "Edit Watch Schedule" if schedule and schedule.id else "Add Watch Schedule",
            [("crew_member_id", "Crew Member *", "combo"), ("vessel_id", "Vessel *", "combo"),
             ("date", "Date", str), ("start_time", "Start (HH:MM)", str),
             ("end_time", "End (HH:MM)", str), ("role_on_watch", "Role", "combo"),
             ("notes", "Notes", "textview")])
        self._db = db
        crew = db.get_crew_members()
        self._cmap = {("%s %s" % (c['first_name'], c['last_name'])): c['id'] for c in crew}
        self._rcmap = {v: k for k, v in self._cmap.items()}
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("crew_member_id", list(self._cmap.keys()))
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self.set_combo("role_on_watch", self.ROLES)
        self._wid = schedule.id if schedule else None
        if schedule:
            cname = self._rcmap.get(schedule.crew_member_id, "")
            self.set_combo("crew_member_id", list(self._cmap.keys()), cname)
            vname = self._rvmap.get(schedule.vessel_id, "")
            self.set_combo("vessel_id", list(self._vmap.keys()), vname)
            self.set_val("date", schedule.date)
            self.set_val("start_time", schedule.start_time)
            self.set_val("end_time", schedule.end_time)
            self.set_combo("role_on_watch", self.ROLES, schedule.role_on_watch)
            self.set_val("notes", schedule.notes)
    def get_watch_schedule(self):
        cname = self.get_val("crew_member_id")
        cid = self._cmap.get(cname, 0)
        vname = self.get_val("vessel_id")
        vid = self._vmap.get(vname, 0)
        return WatchSchedule(id=self._wid, crew_member_id=cid, vessel_id=vid,
            date=self.get_val("date"), start_time=self.get_val("start_time"),
            end_time=self.get_val("end_time"), role_on_watch=self.get_val("role_on_watch"),
            notes=self.get_val("notes"))

class PreferencesDialog(Gtk.Dialog):
    def __init__(self, parent, config, db):
        super().__init__(title="Preferences", transient_for=parent, flags=0, modal=True)
        self.set_default_size(520, 420)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self._config = config
        self._db = db
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        notebook = Gtk.Notebook()
        self._entries = {}

        # General tab
        gen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gen.set_border_width(12)
        for key, label, default, typ in [
            ("due_soon_days", "Due-soon threshold (days)", str(config.get("due_soon_days", 7)), str),
            ("backup_retention_days", "Backup retention (days)", str(config.get("backup_retention_days", 30)), str),
            ("backup_interval_hours", "Backup interval (hours)", str(config.get("backup_interval_hours", 24)), str),
            ("auto_backup", "Auto backup", ["No", "Yes"][config.get("auto_backup", True)], 'combo'),
            ("data_retention_days", "Data retention (days)", str(config.get("data_retention_days", 730)), str),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_size_request(200, -1)
            row.pack_start(lbl, False, False, 0)
            if typ == str:
                w = Gtk.Entry()
                w.set_text(default)
                w.set_width_chars(15)
            elif typ == 'combo':
                w = Gtk.ComboBoxText()
                for val in ["No", "Yes"]:
                    w.append(val, val)
                w.set_active(0 if default == "No" else 1)
            row.pack_start(w, False, False, 0)
            gen.pack_start(row, False, False, 0)
            self._entries[key] = w
        notebook.append_page(gen, Gtk.Label(label="General"))

        # Security tab (ISO 27001 compliance)
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sec.set_border_width(12)
        sec_lbl = Gtk.Label()
        sec_lbl.set_markup("<b>Security & Compliance Settings</b>")
        sec.pack_start(sec_lbl, False, False, 0)
        for key, label, default, typ in [
            ("session_timeout_minutes", "Session timeout (minutes)", str(config.get("session_timeout_minutes", 30)), str),
            ("audit_retention_days", "Audit log retention (days)", str(config.get("audit_retention_days", 365)), str),
            ("require_pin", "Require PIN to open", ["No", "Yes"][config.get("require_pin", False)], 'combo'),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_size_request(200, -1)
            row.pack_start(lbl, False, False, 0)
            if typ == str:
                w = Gtk.Entry()
                w.set_text(default)
                w.set_width_chars(15)
            elif typ == 'combo':
                w = Gtk.ComboBoxText()
                for val in ["No", "Yes"]:
                    w.append(val, val)
                w.set_active(0 if default == "No" else 1)
            row.pack_start(w, False, False, 0)
            sec.pack_start(row, False, False, 0)
            self._entries[key] = w
        # PIN entry (shown conditionally)
        self._pin_entry = Gtk.Entry()
        self._pin_entry.set_placeholder_text("Enter new PIN (4-8 digits)")
        self._pin_entry.set_visibility(False)
        self._pin_entry.set_width_chars(15)
        pin_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pin_row.pack_start(Gtk.Label(label="PIN Code:", xalign=0), False, False, 0)
        pin_row.pack_start(self._pin_entry, False, False, 0)
        sec.pack_start(pin_row, False, False, 0)
        notebook.append_page(sec, Gtk.Label(label="Security"))

        # Financial tab
        fin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        fin.set_border_width(12)
        fin_lbl = Gtk.Label()
        fin_lbl.set_markup("<b>Financial Settings</b>")
        fin.pack_start(fin_lbl, False, False, 0)
        for key, label, default, typ in [
            ("currency", "Default currency", config.get("currency", "USD"), str),
            ("gst_rate", "Default GST/VAT rate (%)", str(config.get("gst_rate", 0.0)), str),
            ("fiscal_year_start", "Fiscal year start (MM-DD)", config.get("fiscal_year_start", "01-01"), str),
            ("company_name", "Company name", config.get("company_name", ""), str),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_size_request(200, -1)
            row.pack_start(lbl, False, False, 0)
            w = Gtk.Entry()
            w.set_text(default)
            w.set_width_chars(20)
            row.pack_start(w, False, False, 0)
            fin.pack_start(row, False, False, 0)
            self._entries[key] = w
        notebook.append_page(fin, Gtk.Label(label="Financial"))

        # Storage tab
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_border_width(12)
        backup_btn = Gtk.Button(label="Show Backup Location")
        backup_btn.connect("clicked", lambda x: self._show_info())
        btn_box.pack_start(backup_btn, False, False, 0)
        audit_btn = Gtk.Button(label="Show Data Directory")
        audit_btn.connect("clicked", lambda x: self._show_data_dir())
        btn_box.pack_start(audit_btn, False, False, 0)
        notebook.append_page(btn_box, Gtk.Label(label="Storage"))
        box.pack_start(notebook, True, True, 0)
        self.show_all()

    def _show_info(self):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text="Backup Information")
        d.format_secondary_text("Backups stored in:\n%s\n\nEmergency backups in:\n%s"
                                % (BACKUP_DIR, os.path.join(BACKUP_DIR, "emergency")))
        d.run()
        d.destroy()

    def _show_data_dir(self):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text="Data Directory")
        d.format_secondary_text("Configuration: %s\nDatabase: %s\nLogs: %s\nReports: %s"
                                % (CONFIG_DIR, DATABASE_FILE, LOG_DIR, REPORTS_DIR))
        d.run()
        d.destroy()

    def apply(self):
        try:
            self._config.set("due_soon_days", max(1, int(self._entries["due_soon_days"].get_text())))
            self._config.set("backup_retention_days", max(1, int(self._entries["backup_retention_days"].get_text())))
            self._config.set("backup_interval_hours", max(1, int(self._entries["backup_interval_hours"].get_text())))
            self._config.set("auto_backup", self._entries["auto_backup"].get_active_text() == "Yes")
            self._config.set("data_retention_days", max(1, int(self._entries["data_retention_days"].get_text())))
            self._config.set("session_timeout_minutes", max(1, int(self._entries["session_timeout_minutes"].get_text())))
            self._config.set("audit_retention_days", max(1, int(self._entries["audit_retention_days"].get_text())))
            self._config.set("require_pin", self._entries["require_pin"].get_active_text() == "Yes")
            self._config.set("currency", self._entries["currency"].get_text())
            self._config.set("company_name", self._entries["company_name"].get_text())
            self._config.set("fiscal_year_start", self._entries["fiscal_year_start"].get_text())
            try:
                gst = float(self._entries["gst_rate"].get_text() or 0)
                self._config.set("gst_rate", gst)
                try:
                    self._db.set_setting("gst_rate", str(gst))
                except Exception as e:
                    logger.warning("Failed to save GST rate to DB: %s", e)
            except ValueError:
                pass
            pin = self._pin_entry.get_text().strip()
            if pin and self._entries["require_pin"].get_active_text() == "Yes":
                if len(pin) < 4 or len(pin) > 8 or not pin.isdigit():
                    show_error(self, "PIN must be 4-8 digits")
                    return
                self._config.set("pin_hash", str(hash(pin)))
            self._config.save()
        except ValueError as e:
            show_error(self, "Invalid number: %s" % e)

class AboutDialog(Gtk.AboutDialog):
    def __init__(self,parent):
        super().__init__(transient_for=parent)
        self.set_program_name("Vessel Keeper"); self.set_version("2.0.0")
        self.set_comments("Enterprise Marine Vessel Management System\nManage vessels, maintenance, inventory, trips, and suppliers.")
        self.set_copyright("Copyright © 2026 Vessel Keeper"); self.set_license("MIT")
        self.set_logo_icon_name("anchor-symbolic"); self.run(); self.destroy()

# =============================================================================
# UI COMPONENTS
# =============================================================================
class SummaryCard(Gtk.EventBox):
    def __init__(self,title,value,subtitle="",color="#667eea"):
        super().__init__()
        self._value_label=None
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4); box.set_border_width(16)
        tl=Gtk.Label(label=title); tl.get_style_context().add_class("card-title"); tl.set_xalign(0); box.pack_start(tl,False,False,0)
        self._value_label=Gtk.Label(label=value); self._value_label.get_style_context().add_class("card-value"); self._value_label.set_xalign(0); box.pack_start(self._value_label,False,False,0)
        if subtitle:
            sl=Gtk.Label(label=subtitle); sl.get_style_context().add_class("card-subtitle"); sl.set_xalign(0); box.pack_start(sl,False,False,0)
        self.add(box)
        css_cls=f"card-{title.lower().replace(' ','-').replace('—','dash')}"
        p=Gtk.CssProvider(); p.load_from_data(f".{css_cls} {{background-color:{color};border-radius:8px;margin:4px;}}".encode())
        self.get_style_context().add_provider(p,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.get_style_context().add_class(css_cls); self.set_property("margin",4); self.set_size_request(200,-1)
    def update(self,val):
        if self._value_label: self._value_label.set_text(val)

class UIStateManager:
    def __init__(self,mqs=1000):
        self._actions=Queue(mqs); self._proc=False; self._lock=threading.RLock(); self._over=0; self._shutdown=False
    def request_shutdown(self): self._shutdown=True
    def queue(self,fn,*a,**kw):
        if self._shutdown: return
        try:
            self._actions.put((fn, a, kw), timeout=0.1)
        except Full:
            self._over += 1
            if self._over % 100 == 0:
                logger.warning("Queue overflow: %d dropped", self._over)
            return
        with self._lock:
            if not self._proc: self._proc=True; GLib.idle_add(self._process)
    def _process(self):
        if self._shutdown: return False
        try:
            while True:
                fn,a,kw=self._actions.get_nowait()
                try: fn(*a,**kw)
                except Exception as e:
                    logger.error("UI action failed: %s", e)
        except Empty: pass
        with self._lock:
            if self._actions.empty() or self._shutdown: self._proc=False; return False
            else: return True

class RefreshManager:
    def __init__(self,app):
        self.app=app; self._lock=threading.RLock(); self._refreshing=False; self._pending=False; self._last=0; self._min=1.0
    def schedule(self):
        now=time.time()
        with self._lock:
            if self._refreshing: self._pending=True; return
            if now-self._last<self._min: self._pending=True; return
            self._refreshing=True
        def _run():
            try: self._do()
            except Exception as e:
                logger.error("Refresh failed: %s", e)
            finally:
                with self._lock:
                    self._refreshing=False; self._last=now
                    if self._pending: self._pending=False; GLib.timeout_add(100,self.schedule)
        threading.Thread(target=_run,daemon=True).start()
    def _do(self):
        app=self.app
        if app._shutting: return
        v = app.db.get_vessels()
        inv = app.db.get_items()
        tasks = app.db.get_tasks_with_vessels()
        isum = app.db.inventory_summary()
        due_days = app.config.get("due_soon_days", 7)
        tc = app.db.task_counts(due_days)
        vc = app.db.vessel_count()
        trips = app.db.get_trips()
        suppliers = app.db.get_suppliers()
        stock = app.db.get_transactions()
        expenses = app.db.get_expenses()
        finsum = app.db.financial_summary()
        feff = app.db.fuel_efficiency()
        crew = app.db.get_crew_members()
        cc = app.db.crew_counts()
        certs = app.db.get_certifications()
        templates = app.db.get_task_templates()
        training = app.db.get_training_records()
        watch = app.db.get_watch_schedules()
        app.ui.queue(app._update_views, v, inv, tasks, isum, tc, vc, trips, suppliers, stock, expenses, finsum, feff, crew, cc, certs, templates, training, watch)

# =============================================================================
# MAIN APPLICATION
# =============================================================================
class VesselKeeperApp(Gtk.Window):
    def __init__(self,db=None,config=None):
        super().__init__(title="Vessel Keeper - Professional Edition")
        self.config=config or ConfigManager()
        self.db=db or ThreadSafeDatabase()
        self.ui=UIStateManager()
        self._shutting=False; self._shut_done=False
        self._last_activity = time.time()
        self.set_default_size(self.config.get("window_width",1200),self.config.get("window_height",800))
        self._refresh_mgr=RefreshManager(self); self._sb=None; self._setup_ui(); self._apply_css(); self._restore_pos(); self._start_bg(); GLib.idle_add(self.refresh)
        self._setup_accels()
        self.connect("key-press-event", lambda *_: self._mark_activity())
        self.connect("button-press-event", lambda *_: self._mark_activity())
        if not self._check_pin_at_startup():
            GLib.idle_add(self._shutdown)
            return
        self._start_session_timeout()
        logger.info("Application initialized")

    def _mark_activity(self):
        self._last_activity = time.time()

    def _setup_accels(self):
        accel_group = Gtk.AccelGroup()
        accel_group.connect(Gdk.keyval_from_name("F5"), 0, Gtk.AccelFlags.VISIBLE, lambda *_: self.refresh())
        accel_group.connect(Gdk.keyval_from_name("Delete"), 0, Gtk.AccelFlags.VISIBLE, lambda *_: self._on_delete_current())
        accel_group.connect(Gdk.keyval_from_name("e"), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE, lambda *_: self._edit_current())
        accel_group.connect(Gdk.keyval_from_name("d"), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE, lambda *_: self._on_delete_current())
        self.add_accel_group(accel_group)

    def _check_pin_at_startup(self):
        if self.config.get("require_pin", False):
            pin_hash = self.config.get("pin_hash", "")
            if not pin_hash:
                return True
            d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.OK_CANCEL, text="PIN Required")
            d.format_secondary_text("Enter your PIN to unlock the application")
            entry = Gtk.Entry()
            entry.set_visibility(False)
            entry.set_input_purpose(Gtk.InputPurpose.PIN)
            d.get_content_area().pack_start(entry, False, False, 8)
            d.show_all()
            authenticated = False
            while d.run() in (Gtk.ResponseType.OK, Gtk.ResponseType.APPLY):
                if str(hash(entry.get_text().strip())) == pin_hash:
                    authenticated = True
                    break
                entry.set_text("")
                d.format_secondary_text("Incorrect PIN. Try again.")
            d.destroy()
            if not authenticated:
                logger.warning("PIN authentication failed")
                return False
            self._last_activity = time.time()
        return True

    def _start_session_timeout(self):
        if hasattr(self, '_session_timer_id') and self._session_timer_id:
            GLib.source_remove(self._session_timer_id)
        timeout_min = self.config.get("session_timeout_minutes", 30)
        if timeout_min <= 0:
            return

        def check():
            if self._shutting:
                return False
            elapsed = time.time() - self._last_activity
            if elapsed > timeout_min * 60:
                logger.info("Session timed out after %d minutes of inactivity", timeout_min)
                self._lock_screen()
                return False
            return True

        self._session_timer_id = GLib.timeout_add_seconds(30, check)

    def _lock_screen(self):
        if hasattr(self, '_session_timer_id') and self._session_timer_id:
            GLib.source_remove(self._session_timer_id)
            self._session_timer_id = None
        self.hide()
        if not self._check_pin_at_startup():
            self._shutdown()
            return
        self._last_activity = time.time()
        self.show_all()
        self._start_session_timeout()

    def _on_delete_current(self):
        page = self.notebook.get_current_page()
        if page == 1: self._del_vessel()
        elif page == 2: self._del_inventory()
        elif page == 3: self._del_task()
        elif page == 4: self._del_trip()
        elif page == 5: self._del_crew_member()
        elif page == 6: self._del_training()
        elif page == 7: self._del_watch()
        elif page == 8: self._del_supplier()
        elif page == 9: self._del_stock()
        elif page == 10: self._del_expense()

    def _setup_menu_actions(self):
        group = Gio.SimpleActionGroup()
        actions = [
            ("export", lambda: self._export_dialog()),
            ("backup", lambda: self._manual_backup()),
            ("prefs", lambda: self._show_prefs()),
            ("gdpr_export", lambda: self._gdpr_export()),
            ("gdpr_delete", lambda: self._gdpr_delete()),
            ("audit_log", lambda: self._show_audit_log()),
            ("templates", lambda: self._manage_templates()),
            ("about", lambda: AboutDialog(self)),
            ("quit", lambda: self._shutdown()),
        ]
        for name, cb in actions:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda a, p, cb=cb: cb())
            group.add_action(act)
        self.insert_action_group("app", group)
        m = Gio.Menu()
        fs = Gio.Menu()
        fs.append("Export to CSV", "app.export")
        fs.append("Backup Database", "app.backup")
        fs.append("Preferences", "app.prefs")
        m.append_section(None, fs)
        ds = Gio.Menu()
        ds.append("Export All Data (GDPR)", "app.gdpr_export")
        ds.append("Delete All Data (GDPR)", "app.gdpr_delete")
        ds.append("View Audit Log", "app.audit_log")
        m.append_section("Data Compliance", ds)
        ts = Gio.Menu()
        ts.append("Manage Task Templates", "app.templates")
        m.append_section("Workflow", ts)
        hs = Gio.Menu()
        hs.append("About", "app.about")
        hs.append("Quit", "app.quit")
        m.append_section(None, hs)
        return m

    def _export_dialog(self):
        dlg=Gtk.FileChooserDialog(title="Export Data",transient_for=self,action=Gtk.FileChooserAction.SAVE)
        dlg.add_button("Cancel",Gtk.ResponseType.CANCEL); dlg.add_button("Save",Gtk.ResponseType.OK)
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Select data to export:"),False,False,0)
        combo=Gtk.ComboBoxText()
        for entity in ["vessels","inventory","tasks","trips","suppliers"]: combo.append(entity,entity)
        combo.set_active(0); box.pack_start(combo,False,False,0)
        dlg.set_extra_widget(box)
        if dlg.run()==Gtk.ResponseType.OK:
            path=dlg.get_filename(); entity=combo.get_active_text()
            try:
                n=self.db.export_csv(path,EXPORT_QUERIES[entity])
                self._status(f"Exported {n} {entity} to {os.path.basename(path)}")
            except Exception as e: show_error(self,str(e))
        dlg.destroy()

    def _show_prefs(self):
        d = PreferencesDialog(self, self.config, self.db)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
        d.destroy()

    def _gdpr_export(self):
        dlg = Gtk.FileChooserDialog(
            title="Export All Data (GDPR)", transient_for=self,
            action=Gtk.FileChooserAction.SAVE)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Save", Gtk.ResponseType.OK)
        dlg.set_current_name("vessel_keeper_gdpr_export.json")
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            try:
                data = self.db.export_all_user_data()
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, default=str)
                self._status("GDPR data export saved to %s" % os.path.basename(path))
            except Exception as e:
                show_error(self, "Export failed: %s" % e)
        dlg.destroy()

    def _gdpr_delete(self):
        if not confirm(self, "Permanently delete ALL data?",
                       "This action cannot be undone. All vessels, expenses, trips, and settings will be removed."):
            return
        if not confirm(self, "Are you absolutely sure?",
                       "This is a GDPR right-to-erasure operation. All your data will be permanently deleted."):
            return
        try:
            self.db.delete_all_user_data()
            self.refresh()
            self._status("All data deleted per GDPR right to erasure")
        except Exception as e:
            show_error(self, "Data deletion failed: %s" % e)

    def _show_audit_log(self):
        dlg = Gtk.Dialog(title="Audit Log", transient_for=self, flags=0, modal=True)
        dlg.set_default_size(800, 500)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        store = Gtk.ListStore(str, str, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, n in [(0, "Time"), (1, "Action"), (2, "Entity"), (3, "Entity ID"), (4, "Session")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        try:
            rows = self.db.get_audit_log(500)
            for r in rows:
                store.append([r.get('timestamp', ''), r.get('action', ''),
                              r.get('entity_type', ''), str(r.get('entity_id', '') or ''),
                              r.get('session_id', '')])
        except Exception as e:
            store.append(["Error", str(e), "", "", ""])
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        box = dlg.get_content_area()
        box.pack_start(sw, True, True, 0)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _manage_templates(self):
        dlg = Gtk.Dialog(title="Manage Task Templates", transient_for=self, flags=0, modal=True)
        dlg.set_default_size(600, 400)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)

        tb = Gtk.Toolbar()
        add_btn = Gtk.ToolButton(label="Add Template", icon_name="list-add-symbolic")
        add_btn.connect("clicked", lambda x: self._add_template(dlg))
        tb.insert(add_btn, -1)
        del_btn = Gtk.ToolButton(label="Delete", icon_name="edit-delete-symbolic")
        del_btn.connect("clicked", lambda x: self._del_template(dlg))
        tb.insert(del_btn, -1)
        box.pack_start(tb, False, False, 0)

        store = Gtk.ListStore(int, str, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, n in [(1, "Name"), (2, "System"), (3, "Priority"), (4, "Interval")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        dlg.show_all()

        # Load templates
        for t in getattr(self, '_all_templates', []) or self.db.get_task_templates():
            interval = ""
            if t.get('interval_days'):
                interval = "%d days" % t['interval_days']
            elif t.get('interval_hours'):
                interval = "%.1f hrs" % t['interval_hours']
            store.append([t['id'], t['name'], t.get('system', ''), t.get('priority', ''), interval])

        dlg.run()
        dlg.destroy()

    def _add_template(self, parent):
        d = TemplateDialog(parent, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.add_task_template(d.get_template())
                self.refresh()
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _del_template(self, parent):
        dlg = parent
        # Find the list store from the dialog
        for child in dlg.get_content_area().get_children():
            if isinstance(child, Gtk.ScrolledWindow):
                tv = child.get_child()
                if isinstance(tv, Gtk.TreeView):
                    sel = tv.get_selection()
                    _, ti = sel.get_selected()
                    if not ti:
                        return
                    model = tv.get_model()
                    tid = model.get_value(ti, 0)
                    name = model.get_value(ti, 1)
                    if confirm(dlg, "Delete template '%s'?" % name):
                        try:
                            self.db.delete_task_template(tid)
                            self.refresh()
                            model.remove(ti)
                        except DatabaseError as e:
                            show_error(self, str(e))
                    return

    def _setup_ui(self):
        try:
            mv=Gtk.Box(orientation=Gtk.Orientation.VERTICAL); self.add(mv)
            hb=Gtk.HeaderBar(); hb.set_show_close_button(True)
            hb.set_title("Vessel Keeper"); hb.set_subtitle("Professional Marine Management System")

            ref=Gtk.Button(); ref.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic",Gtk.IconSize.BUTTON)); ref.set_tooltip_text("Refresh (F5)"); ref.connect("clicked",lambda x:self.refresh()); hb.pack_start(ref)
            backup=Gtk.Button(); backup.set_image(Gtk.Image.new_from_icon_name("document-save-symbolic",Gtk.IconSize.BUTTON)); backup.set_tooltip_text("Backup DB"); backup.connect("clicked",lambda x:self._manual_backup()); hb.pack_start(backup)

            mb=Gtk.MenuButton(); mi=Gtk.Image.new_from_icon_name("open-menu-symbolic",Gtk.IconSize.BUTTON); mb.set_image(mi); mb.set_tooltip_text("Menu")
            mb.set_menu_model(self._setup_menu_actions()); hb.pack_end(mb)
            self.set_titlebar(hb)

            self.notebook=Gtk.Notebook(); self.notebook.set_scrollable(True)
            self.notebook.append_page(self._dash_tab(),Gtk.Label(label="  Dashboard  "))
            self.notebook.append_page(self._vessel_tab(),Gtk.Label(label="  Vessels  "))
            self.notebook.append_page(self._inventory_tab(),Gtk.Label(label="  Inventory  "))
            self.notebook.append_page(self._mtab(),Gtk.Label(label="  Maintenance  "))
            self.notebook.append_page(self._trip_tab(),Gtk.Label(label="  Trip Logs  "))
            self.notebook.append_page(self._crew_tab(), Gtk.Label(label="  Crew  "))
            self.notebook.append_page(self._training_tab(), Gtk.Label(label="  Training  "))
            self.notebook.append_page(self._watch_tab(), Gtk.Label(label="  Watch Schedules  "))
            self.notebook.append_page(self._supplier_tab(),Gtk.Label(label="  Suppliers  "))
            self.notebook.append_page(self._stock_tab(), Gtk.Label(label="  Stock History  "))
            self.notebook.append_page(self._expense_tab(), Gtk.Label(label="  Expenses  "))
            self.notebook.append_page(self._finance_tab(), Gtk.Label(label="  Financial Reports  "))
            mv.pack_start(self.notebook, True, True, 0)

            self._sb=Gtk.Statusbar(); self._sbc=self._sb.get_context_id("main"); mv.pack_end(self._sb,False,False,0)
            self._status("Ready")
        except Exception as e:
            logger.error("UI setup: %s", e)
            raise ResourceError("UI failed: %s" % e)

    def _make_toolbar(self, buttons, search_cb=None):
        tb=Gtk.Toolbar(); tb.get_style_context().add_class(Gtk.STYLE_CLASS_TOOLBAR)
        for label,icon,cb in buttons:
            b=Gtk.ToolButton(label=label,icon_name=icon); b.connect("clicked",lambda x,ccb=cb:ccb()); tb.insert(b,-1)
        if search_cb:
            tb.insert(Gtk.SeparatorToolItem(),-1)
            se=Gtk.Entry(); se.set_placeholder_text("Search..."); se.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY,"edit-find-symbolic")
            si=Gtk.ToolItem(); si.add(se); tb.insert(si,-1)
            se.connect("changed",lambda e: search_cb(e.get_text().lower()))
            return tb,se
        return tb,None

    def _treeview(self,store):
        tv=Gtk.TreeView(model=store); tv.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        tv.connect("button-press-event",self._right_click)
        return tv

    def _right_click(self,w,e):
        if e.button==3:
            sel=w.get_selection()
            if sel:
                _,ti=sel.get_selected()
                if ti:
                    me=Gtk.Menu()
                    ed=Gtk.MenuItem(label="Edit"); ed.connect("activate",lambda *_:self._edit_current())
                    me.append(ed)
                    dl=Gtk.MenuItem(label="Delete"); dl.connect("activate",lambda *_:self._on_delete_current())
                    me.append(dl)
                    me.show_all(); me.popup_at_pointer(e)

    def _edit_current(self, w=None):
        p = self.notebook.get_current_page()
        if p == 1: self._edit_vessel()
        elif p == 2: self._edit_inventory()
        elif p == 3: self._edit_task()
        elif p == 4: self._edit_trip()
        elif p == 5: self._edit_crew_member()
        elif p == 6: self._edit_training()
        elif p == 7: self._edit_watch()
        elif p == 8: self._edit_supplier()
        elif p == 9: pass  # Stock History — read-only
        elif p == 10: self._edit_expense()

    def _delete_current(self, w=None):
        self._on_delete_current()

    def _empty_state(self, text):
        lbl = Gtk.Label()
        lbl.set_markup("<span size='large' color='#6c757d'><i>%s</i></span>" % text)
        lbl.get_style_context().add_class("empty-state")
        return lbl

    # ---- DASHBOARD ----
    def _dash_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        b.set_border_width(12)
        h = Gtk.Label(label="Dashboard — Overview & Quick Actions")
        h.get_style_context().add_class("professional-header")
        b.pack_start(h, False, False, 0)

        # Empty-state welcome panel (hidden when data exists)
        self._welcome_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._welcome_box.set_border_width(20)
        wel = Gtk.Label()
        wel.set_markup("<big><b>Welcome to Vessel Keeper</b></big>")
        self._welcome_box.pack_start(wel, False, False, 0)
        sub = Gtk.Label(label="Get started by adding vessels, inventory items, or maintenance tasks.")
        self._welcome_box.pack_start(sub, False, False, 0)
        sample_btn = Gtk.Button(label="Load Sample Data")
        sample_btn.set_image(Gtk.Image.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.BUTTON))
        sample_btn.set_always_show_image(True)
        sample_btn.get_style_context().add_class("suggested-action")
        sample_btn.connect("clicked", lambda x: self._seed_sample_data())
        self._welcome_box.pack_start(sample_btn, False, False, 0)
        b.pack_start(self._welcome_box, False, False, 0)

        # Summary cards
        cb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cb.set_homogeneous(True)
        self._cd_v = SummaryCard("Vessels", "—", "Total registered", "#667eea")
        self._cd_t = SummaryCard("Tasks Due", "—", "Next 7 days", "#e67e22")
        self._cd_s = SummaryCard("Low Stock", "—", "Items below minimum", "#e74c3c")
        self._cd_o = SummaryCard("Overdue", "—", "Past-due tasks", "#c0392b")
        self._cd_tx = SummaryCard("Transactions", "—", "Stock movements", "#2c3e50")
        self._cd_vl = SummaryCard("Inventory Value", "$0", "Total stock value", "#27ae60")
        self._cd_crew = SummaryCard("Active Crew", "—", "Current members", "#1abc9c")
        self._cd_certs = SummaryCard("Expiring Certs", "—", "Next 30 days", "#f39c12")
        self._cd_expired = SummaryCard("Expired Certs", "—", "Overdue renewal", "#e74c3c")
        for c in (self._cd_v, self._cd_t, self._cd_s, self._cd_o, self._cd_tx, self._cd_vl, self._cd_crew, self._cd_certs, self._cd_expired):
            cb.pack_start(c, True, True, 0)
        b.pack_start(cb, False, False, 0)

        # Quick actions
        af = Gtk.Frame(label="Quick Actions")
        ab = Gtk.FlowBox()
        ab.set_border_width(12)
        ab.set_max_children_per_line(6)
        ab.set_selection_mode(Gtk.SelectionMode.NONE)
        for lbl, icon, cb2 in [
            ("Add Vessel", "list-add-symbolic", self._add_vessel),
            ("New Task", "appointment-new-symbolic", self._add_task),
            ("Add Item", "insert-object-symbolic", self._add_inventory),
            ("New Trip", "media-record-symbolic", self._add_trip),
            ("Add Supplier", "contact-new-symbolic", self._add_supplier),
            ("Refresh", "view-refresh-symbolic", self.refresh),
        ]:
            btn = Gtk.Button(label=lbl)
            btn.get_style_context().add_class("quick-action-btn")
            btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            btn.set_always_show_image(True)
            btn.connect("clicked", lambda x, ccb=cb2: ccb())
            ab.add(btn)
        af.add(ab)
        b.pack_start(af, False, False, 0)

        # Recent activity
        rf = Gtk.Frame(label="Recent Activity")
        self._rv = Gtk.ListStore(str, str, str)
        rv = Gtk.TreeView(model=self._rv)
        rv.set_vexpand(False)
        for i, n in [(0, "Time"), (1, "Entity"), (2, "Action")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            rv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(120)
        sw.add(rv)
        rf.add(sw)
        b.pack_start(rf, True, True, 0)

        # Helpful tips
        tip = Gtk.Label()
        tip.set_markup(
            "\n<span color='#6c757d'><i>Tip: Use <b>Ctrl+E</b> to edit, <b>Ctrl+D</b> to delete, <b>Ctrl+F</b> to search, <b>F5</b> to refresh</i></span>"
        )
        b.pack_start(tip, False, False, 0)
        return b

    def _seed_sample_data(self):
        """Populate database with sample data for demonstration."""
        try:
            v1_id=self.db.add_vessel(Vessel(name="Sea Voyager",vessel_type="Yacht",engine_make="Caterpillar",engine_model="C32",current_location="Miami"))
            v2_id=self.db.add_vessel(Vessel(name="Oceanic Star",vessel_type="Fishing Trawler",engine_make="Cummins",engine_model="QSK19",current_location="Seattle"))
            v3_id=self.db.add_vessel(Vessel(name="Harbor Master",vessel_type="Tugboat",engine_make="MTU",engine_model="16V4000",current_location="Rotterdam"))
            self.db.add_vessel(Vessel(name="Coastal Runner",vessel_type="Speedboat",engine_make="Mercury",engine_model="Verado 400",current_location="Sydney"))

            self.db.add_item("Engine Oil 15W-40","EO-15W40","Lubricants",50,20,12.50)
            self.db.add_item("Fuel Filter FF-532","FF-532","Filters",30,10,18.75)
            self.db.add_item("Anchor Chain 16mm","AC-16","Rigging",5,2,450.00)
            self.db.add_item("Navigation Light LED","NL-100","Electrical",12,5,85.00)
            self.db.add_item("Life Jacket Type I","LJ-1","Safety",8,15,120.00)
            self.db.add_item("Marine Battery 12V 200Ah","MB-200","Electrical",4,3,350.00)

            self.db.add_task(MaintenanceTask(vessel_id=v1_id,task_name="Engine Oil Change",system="Propulsion",
                description="Replace oil and filter",next_due=datetime.date.today().isoformat(),priority="high",status="pending"))
            self.db.add_task(MaintenanceTask(vessel_id=v1_id,task_name="Hull Inspection",system="Hull",
                description="Annual hull inspection for cracks",next_due=(datetime.date.today()+datetime.timedelta(days=30)).isoformat(),priority="medium",status="pending"))
            self.db.add_task(MaintenanceTask(vessel_id=v2_id,task_name="Net Repair",system="Deck",
                description="Repair torn fishing net sections",next_due=datetime.date.today().isoformat(),priority="critical",status="overdue"))
            self.db.add_task(MaintenanceTask(vessel_id=v3_id,task_name="Engine Overhaul",system="Propulsion",
                description="Complete 5000-hour engine overhaul",next_due=(datetime.date.today()+datetime.timedelta(days=14)).isoformat(),priority="high",status="pending"))

            self.db.add_supplier(Supplier(name="Marine Parts Co.",contact="John Smith",phone="+1-305-555-0142",email="john@marineparts.com",preferred=True))
            self.db.add_supplier(Supplier(name="Oceanic Supplies Ltd.",contact="Sarah Jones",phone="+1-206-555-0187",email="sjones@oceanic.com",preferred=False))

            self.db.add_trip(TripLog(vessel_id=v1_id,departure="2026-05-10 08:00",arrival="2026-05-10 17:30",destination="Key West",distance=160.0,fuel_used=320.0))
            self.db.add_trip(TripLog(vessel_id=v2_id,departure="2026-05-11 05:00",arrival="2026-05-11 19:00",destination="Pacific Grounds",distance=280.0,fuel_used=540.0))

            self.refresh()
            self._status("Sample data loaded — click Refresh to see changes")
        except (ValidationError,DatabaseError) as e:
            show_error(self, "Failed to load sample data: %s" % e)

    # ---- VESSELS ----
    def _vessel_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_vessel),
            ("Edit", "document-edit-symbolic", self._edit_vessel),
            ("Delete", "edit-delete-symbolic", self._del_vessel),
            ("Export", "document-save-as-symbolic", lambda: self._export("vessels")),
        ], search_cb=lambda t: self._filter_vessels(t))
        self._vessel_search = se
        b.pack_start(tb, False, False, 0)

        self._vs = Gtk.ListStore(int, str, str, str, str, str)
        tv = self._treeview(self._vs)
        for i, n in [(1, "Name"), (2, "Type"), (3, "Engine Make"), (4, "Engine Model"), (5, "Location")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda *_: self._edit_vessel())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._vessel_empty = self._empty_state("No vessels yet. Click Add to register one.")
        self._vessel_empty.set_no_show_all(True)
        b.pack_start(self._vessel_empty, False, False, 0)
        self._vtv = tv
        return b

    def _filter_vessels(self, t):
        self._vs.clear()
        data = getattr(self, '_all_vessels', [])
        filtered = 0
        for v in data:
            if not t or any(t in str(v.get(k, '')).lower() for k in ('name', 'vessel_type', 'engine_make', 'engine_model', 'current_location')):
                self._vs.append([v.get('id', 0), v.get('name', ''), v.get('vessel_type', ''), v.get('engine_make', ''), v.get('engine_model', ''), v.get('current_location', '')])
                filtered += 1
        if hasattr(self, '_vessel_empty'):
            self._vessel_empty.set_visible(filtered == 0)

    # ---- INVENTORY ----
    def _inventory_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_inventory),
            ("Edit", "document-edit-symbolic", self._edit_inventory),
            ("Delete", "edit-delete-symbolic", self._del_inventory),
            ("Adjust Stock", "document-properties-symbolic", self._adjust_stock),
            ("Export", "document-save-as-symbolic", lambda: self._export("inventory")),
        ], search_cb=lambda t: self._filter_items(t))
        self._inv_search = se
        b.pack_start(tb, False, False, 0)

        self._is = Gtk.ListStore(int, str, str, str, int, int, float)
        tv = self._treeview(self._is)
        for i, n in [(1, "Name"), (2, "Part #"), (3, "Category"), (4, "Stock"), (5, "Min"), (6, "Unit Cost")]:
            r = Gtk.CellRendererText()
            if i in (4, 5, 6):
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            if i == 6:
                c = Gtk.TreeViewColumn("Unit Cost", Gtk.CellRendererText())
                c.set_cell_data_func(Gtk.CellRendererText(), lambda col, cell, model, itr, d: cell.set_property("text", "$%.2f" % model.get_value(itr, 6) if model.get_value(itr, 6) else "$0.00"))
            tv.append_column(c)

        def _scol(col, cell, model, itr, d):
            s = model.get_value(itr, 4)
            m = model.get_value(itr, 5)
            cell.set_property("cell-background", "#fce4e4" if s <= m else None)
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r, Gtk.CellRendererText):
                    c.set_cell_data_func(r, _scol)

        tv.connect("row-activated", lambda *_: self._edit_inventory())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._inv_empty = self._empty_state("No inventory items. Click Add to create one.")
        self._inv_empty.set_no_show_all(True)
        b.pack_start(self._inv_empty, False, False, 0)
        self._itv = tv
        return b

    def _filter_items(self, t):
        self._is.clear()
        all_items = getattr(self, '_all_items', [])
        filtered = 0
        for item in all_items:
            if not t or any(t in str(item.get(k, '')).lower() for k in ('name', 'part_number', 'category')):
                self._is.append([item.get('id', 0), item.get('name', ''), item.get('part_number', ''), item.get('category', ''), item.get('current_stock', 0), item.get('minimum_stock', 0), item.get('unit_cost', 0.0)])
                filtered += 1
        if hasattr(self, '_inv_empty'):
            self._inv_empty.set_visible(filtered == 0)

    def _adjust_stock(self):
        sel=self._itv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        iid=self._is.get_value(ti,0); name=self._is.get_value(ti,1); cur=self._is.get_value(ti,4)
        d=StockAdjustDialog(self,name,cur)
        if d.run()==Gtk.ResponseType.OK:
            try:
                delta=d.get_delta(); note=d.get_note()
                new=self.db.adjust_stock(iid,delta,note)
                self._status(f"Stock adjusted: {name} ({cur} -> {new})"); self.refresh()
            except (ValidationError,ValueError) as e: show_error(self,str(e))
        d.destroy()

    # ---- MAINTENANCE ----
    def _mtab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_task),
            ("Edit", "document-edit-symbolic", self._edit_task),
            ("Delete", "edit-delete-symbolic", self._del_task),
            ("Complete", "emblem-default-symbolic", self._complete_task),
            ("Parts", "package-symbolic", self._manage_task_parts),
            ("Template", "document-new-symbolic", self._from_template),
            ("Export", "document-save-as-symbolic", lambda: self._export("tasks")),
        ], search_cb=lambda t: self._filter_tasks(t))
        self._task_search = se
        b.pack_start(tb, False, False, 0)

        self._ts = Gtk.ListStore(int, str, str, str, str, str, str, str)
        tv = self._treeview(self._ts)
        for i, n in [(1, "WO"), (2, "Vessel"), (3, "System"), (4, "Task"), (5, "Due"), (6, "Status"), (7, "Priority")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)

        def _tcol(col, cell, model, itr, d):
            st = model.get_value(itr, 6)
            pr = model.get_value(itr, 7)
            bg = None
            if st == "overdue":
                bg = "#fce4e4"
            elif st == "completed":
                bg = "#e8f5e9"
            elif st == "in_progress":
                bg = "#e3f2fd"
            if pr == "critical" and st != "completed":
                bg = "#ffebee"
            cell.set_property("cell-background", bg)
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r, Gtk.CellRendererText):
                    c.set_cell_data_func(r, _tcol)

        tv.connect("row-activated", lambda *_: self._edit_task())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._task_empty = self._empty_state("No maintenance tasks. Click Add to schedule one.")
        self._task_empty.set_no_show_all(True)
        b.pack_start(self._task_empty, False, False, 0)
        self._ttv = tv
        return b

    def _filter_tasks(self, t):
        self._ts.clear()
        tasks = getattr(self, '_all_tasks', [])
        filtered = 0
        for task in tasks:
            wo = task.get('work_order', '') or ''
            if not t or any(t in str(task.get(k, '')).lower() for k in ('vessel_name', 'system', 'task_name', 'status', 'priority', 'work_order')):
                self._ts.append([task.get('id', 0), wo, task.get('vessel_name', ''), task.get('system', ''),
                                 task.get('task_name', ''), task.get('next_due', ''), task.get('status', ''),
                                 task.get('priority', '')])
                filtered += 1
        if hasattr(self, '_task_empty'):
            self._task_empty.set_visible(filtered == 0)

    def _complete_task(self):
        sel = self._ttv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        tid = self._ts.get_value(ti, 0)
        name = self._ts.get_value(ti, 4)
        d = CompleteTaskDialog(self, tid, name)
        if d.run() == Gtk.ResponseType.OK:
            try:
                completed_by, notes, cost = d.get_data()
                self.db.complete_task(tid, completed_by, notes, cost)
                self.refresh()
                self._status("Task completed: %s" % name)
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _manage_task_parts(self):
        sel = self._ttv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        tid = self._ts.get_value(ti, 0)
        task_name = self._ts.get_value(ti, 4)
        d = TaskPartsDialog(self, self.db, tid, task_name)
        d.run()
        d.destroy()

    def _from_template(self):
        if not hasattr(self, '_all_templates') or not self._all_templates:
            show_error(self, "No task templates available. Create templates in the database.")
            return
        names = [t['name'] for t in self._all_templates]
        dlg = Gtk.Dialog(title="Create Task from Template", transient_for=self, flags=0, modal=True)
        dlg.set_default_size(400, 200)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Create", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label="Select template:"), False, False, 0)
        combo = Gtk.ComboBoxText()
        for n in names:
            combo.append(n, n)
        combo.set_active(0)
        box.pack_start(combo, False, False, 0)
        box.pack_start(Gtk.Label(label="Vessel:"), False, False, 0)
        vessel_combo = Gtk.ComboBoxText()
        for v in getattr(self, '_all_vessels', []):
            vessel_combo.append(v['name'], v['name'])
        vessel_combo.set_active(0)
        box.pack_start(vessel_combo, False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            tname = combo.get_active_text()
            vname = vessel_combo.get_active_text()
            tmpl = next((t for t in self._all_templates if t['name'] == tname), None)
            vessel = next((v for v in getattr(self, '_all_vessels', []) if v['name'] == vname), None)
            if tmpl and vessel:
                wo = self.db.next_work_order()
                task = MaintenanceTask(vessel_id=vessel['id'], system=tmpl.get('system', ''),
                    task_name=tmpl['name'], description=tmpl.get('description', ''),
                    next_due=datetime.datetime.now().strftime("%Y-%m-%d"),
                    priority=tmpl.get('priority', 'medium'), work_order=wo,
                    interval_days=tmpl.get('interval_days'), interval_hours=tmpl.get('interval_hours'),
                    is_recurring=tmpl.get('interval_days') is not None)
                try:
                    self.db.add_task(task)
                    self.refresh()
                    self._status("Task created from template: %s (WO: %s)" % (tname, wo))
                except (ValidationError, DatabaseError) as e:
                    show_error(self, str(e))
        dlg.destroy()

    # ---- Task CRUD handlers ----

    def _filter_trips(self, t):
        self._trip_s.clear()
        trips = getattr(self, '_all_trips', [])
        filtered = 0
        for trip in trips:
            if not t or any(t in str(trip.get(k, '')).lower() for k in ('vessel_name', 'destination', 'departure', 'arrival')):
                self._trip_s.append([trip['id'], trip.get('vessel_name', ''), trip.get('destination', ''), trip.get('departure', ''), trip.get('arrival', ''), trip.get('distance', 0.0), trip.get('fuel_used', 0.0)])
                filtered += 1
        if hasattr(self, '_trip_empty'):
            self._trip_empty.set_visible(filtered == 0)

    def _filter_suppliers(self, t):
        self._ss.clear()
        suppliers = getattr(self, '_all_suppliers', [])
        filtered = 0
        for s in suppliers:
            if not t or any(t in str(s.get(k, '')).lower() for k in ('name', 'contact', 'phone', 'email')):
                self._ss.append([s['id'], s['name'], s.get('contact', ''), s.get('phone', ''), s.get('email', ''), "Yes" if s.get('preferred') else "No"])
                filtered += 1
        if hasattr(self, '_supplier_empty'):
            self._supplier_empty.set_visible(filtered == 0)

    # ---- TRIPS ----
    def _trip_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_trip),
            ("Edit", "document-edit-symbolic", self._edit_trip),
            ("Delete", "edit-delete-symbolic", self._del_trip),
            ("Export", "document-save-as-symbolic", lambda: self._export("trips")),
        ], search_cb=lambda t: self._filter_trips(t))
        self._trip_search = se
        b.pack_start(tb, False, False, 0)

        self._trip_s = Gtk.ListStore(int, str, str, str, str, float, float)
        tv = self._treeview(self._trip_s)
        for i, n in [(1, "Vessel"), (2, "Destination"), (3, "Departure"), (4, "Arrival"), (5, "Distance (nm)"), (6, "Fuel Used")]:
            r = Gtk.CellRendererText()
            if i in (5, 6):
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda *_: self._edit_trip())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._trip_empty = self._empty_state("No trip logs. Click Add to record one.")
        self._trip_empty.set_no_show_all(True)
        b.pack_start(self._trip_empty, False, False, 0)
        self._trip_tv = tv
        return b

    # ---- CREW ----
    def _crew_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_crew_member),
            ("Edit", "document-edit-symbolic", self._edit_crew_member),
            ("Delete", "edit-delete-symbolic", self._del_crew_member),
            ("Certs", "security-high-symbolic", self._manage_certs),
            ("Export", "document-save-as-symbolic", lambda: self._export("crew")),
        ], search_cb=lambda t: self._filter_crew(t))
        self._crew_search = se
        b.pack_start(tb, False, False, 0)

        self._crew_s = Gtk.ListStore(int, str, str, str, str, str, str)
        tv = self._treeview(self._crew_s)
        for i, n in [(1, "Name"), (2, "Role"), (3, "Status"), (4, "Phone"), (5, "Email"), (6, "Joined")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)

        def _crow_col(col, cell, model, itr, d):
            st = model.get_value(itr, 3)
            bg = None
            if st == "active":
                bg = "#e8f5e9"
            elif st == "inactive":
                bg = "#fff3e0"
            elif st == "terminated":
                bg = "#fce4e4"
            cell.set_property("cell-background", bg)
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r, Gtk.CellRendererText):
                    c.set_cell_data_func(r, _crow_col)

        tv.connect("row-activated", lambda *_: self._edit_crew_member())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._crew_empty = self._empty_state("No crew members. Click Add to register one.")
        self._crew_empty.set_no_show_all(True)
        b.pack_start(self._crew_empty, False, False, 0)
        self._crew_tv = tv
        return b

    def _filter_crew(self, t):
        self._crew_s.clear()
        data = getattr(self, '_all_crew', [])
        filtered = 0
        for row in data:
            full = "%s %s" % (row.get('first_name', ''), row.get('last_name', ''))
            if not t or any(t in str(row.get(k, '')).lower() for k in ('first_name', 'last_name', 'role', 'email')):
                self._crew_s.append([
                    row['id'], full, row.get('role', ''), row.get('status', ''),
                    row.get('phone', ''), row.get('email', ''), row.get('date_joined', '')
                ])
                filtered += 1
        if hasattr(self, '_crew_empty'):
            self._crew_empty.set_visible(filtered == 0)

    def _add_crew_member(self):
        d = CrewDialog(self, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                c = d.get_crew_member()
                self.db.add_crew_member(c)
                self.refresh()
                self._status("Crew member added")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _edit_crew_member(self):
        sel = self._crew_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        cid = self._crew_s.get_value(ti, 0)
        row = self.db.get_crew_member(cid)
        if not row:
            return
        d = CrewDialog(self, self.db, CrewMember.from_dict(row))
        if d.run() == Gtk.ResponseType.OK:
            try:
                c = d.get_crew_member()
                c.id = cid
                self.db.update_crew_member(c)
                self.refresh()
                self._status("Crew member updated")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _del_crew_member(self):
        sel = self._crew_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        name = self._crew_s.get_value(ti, 1)
        cid = self._crew_s.get_value(ti, 0)
        if confirm(self, "Delete crew member '%s'?" % name):
            try:
                self.db.delete_crew_member(cid)
                self.refresh()
                self._status("Crew member deleted")
            except DatabaseError as e:
                show_error(self, str(e))

    def _manage_certs(self):
        sel = self._crew_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        cid = self._crew_s.get_value(ti, 0)
        name = self._crew_s.get_value(ti, 1)
        d = CertDialog(self, self.db, cid, name)
        d.run()
        d.destroy()
        self.refresh()

    # ---- TRAINING RECORDS ----
    def _training_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_training),
            ("Edit", "document-edit-symbolic", self._edit_training),
            ("Delete", "edit-delete-symbolic", self._del_training),
            ("Export", "document-save-as-symbolic", lambda: self._export("training")),
        ], search_cb=lambda t: self._filter_training(t))
        self._train_search = se
        b.pack_start(tb, False, False, 0)

        self._train_s = Gtk.ListStore(int, str, str, str, str, str, float)
        tv = self._treeview(self._train_s)
        for i, n in [(1, "Crew Member"), (2, "Course"), (3, "Provider"), (4, "Completed"), (5, "Expires"), (6, "Cost")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda *_: self._edit_training())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._train_empty = self._empty_state("No training records. Click Add to record one.")
        self._train_empty.set_no_show_all(True)
        b.pack_start(self._train_empty, False, False, 0)
        self._train_tv = tv
        return b

    def _filter_training(self, t):
        self._train_s.clear()
        data = getattr(self, '_all_training', [])
        filtered = 0
        for row in data:
            if not t or any(t in str(row.get(k, '')).lower() for k in ('course_name', 'provider', 'crew_member')):
                self._train_s.append([
                    row['id'], row.get('crew_member', ''), row.get('course_name', ''),
                    row.get('provider', ''), row.get('date_completed', ''),
                    row.get('expiry_date', ''), row.get('cost', 0.0)
                ])
                filtered += 1
        if hasattr(self, '_train_empty'):
            self._train_empty.set_visible(filtered == 0)

    def _add_training(self):
        d = TrainingDialog(self, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.add_training_record(d.get_training_record())
                self.refresh()
                self._status("Training record added")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _edit_training(self):
        sel = self._train_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        tid = self._train_s.get_value(ti, 0)
        rows = [r for r in getattr(self, '_all_training', []) if r['id'] == tid]
        if not rows:
            return
        row = rows[0]
        rec = TrainingRecord(id=row['id'], crew_member_id=row.get('crew_member_id', 0),
            course_name=row.get('course_name', ''), provider=row.get('provider', ''),
            date_completed=row.get('date_completed', ''), expiry_date=row.get('expiry_date', ''),
            cost=row.get('cost', 0.0), notes=row.get('notes', ''))
        d = TrainingDialog(self, self.db, rec)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.update_training_record(d.get_training_record())
                self.refresh()
                self._status("Training record updated")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _del_training(self):
        sel = self._train_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        tid = self._train_s.get_value(ti, 0)
        if confirm(self, "Delete this training record?"):
            try:
                self.db.delete_training_record(tid)
                self.refresh()
                self._status("Training record deleted")
            except DatabaseError as e:
                show_error(self, str(e))

    # ---- WATCH SCHEDULES ----
    def _watch_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_watch),
            ("Edit", "document-edit-symbolic", self._edit_watch),
            ("Delete", "edit-delete-symbolic", self._del_watch),
            ("Export", "document-save-as-symbolic", lambda: self._export("watch_schedules")),
        ], search_cb=lambda t: self._filter_watch(t))
        self._watch_search = se
        b.pack_start(tb, False, False, 0)

        self._watch_s = Gtk.ListStore(int, str, str, str, str, str, str)
        tv = self._treeview(self._watch_s)
        for i, n in [(1, "Crew"), (2, "Vessel"), (3, "Date"), (4, "Start"), (5, "End"), (6, "Role")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda *_: self._edit_watch())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._watch_empty = self._empty_state("No watch schedules. Click Add to create one.")
        self._watch_empty.set_no_show_all(True)
        b.pack_start(self._watch_empty, False, False, 0)
        self._watch_tv = tv
        return b

    def _filter_watch(self, t):
        self._watch_s.clear()
        data = getattr(self, '_all_watch', [])
        filtered = 0
        for row in data:
            if not t or any(t in str(row.get(k, '')).lower() for k in ('crew_name', 'vessel_name', 'role_on_watch')):
                self._watch_s.append([
                    row['id'], row.get('crew_name', ''), row.get('vessel_name', ''),
                    row.get('date', ''), row.get('start_time', ''),
                    row.get('end_time', ''), row.get('role_on_watch', '')
                ])
                filtered += 1
        if hasattr(self, '_watch_empty'):
            self._watch_empty.set_visible(filtered == 0)

    def _add_watch(self):
        d = WatchScheduleDialog(self, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.add_watch_schedule(d.get_watch_schedule())
                self.refresh()
                self._status("Watch schedule added")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _edit_watch(self):
        sel = self._watch_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        wid = self._watch_s.get_value(ti, 0)
        rows = [r for r in getattr(self, '_all_watch', []) if r['id'] == wid]
        if not rows:
            return
        row = rows[0]
        rec = WatchSchedule(id=row['id'], crew_member_id=row.get('crew_member_id', 0),
            vessel_id=row.get('vessel_id', 0), date=row.get('date', ''),
            start_time=row.get('start_time', ''), end_time=row.get('end_time', ''),
            role_on_watch=row.get('role_on_watch', ''), notes=row.get('notes', ''))
        d = WatchScheduleDialog(self, self.db, rec)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.update_watch_schedule(d.get_watch_schedule())
                self.refresh()
                self._status("Watch schedule updated")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _del_watch(self):
        sel = self._watch_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        wid = self._watch_s.get_value(ti, 0)
        if confirm(self, "Delete this watch schedule?"):
            try:
                self.db.delete_watch_schedule(wid)
                self.refresh()
                self._status("Watch schedule deleted")
            except DatabaseError as e:
                show_error(self, str(e))

    # ---- SUPPLIERS ----
    def _supplier_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_supplier),
            ("Edit", "document-edit-symbolic", self._edit_supplier),
            ("Delete", "edit-delete-symbolic", self._del_supplier),
            ("Export", "document-save-as-symbolic", lambda: self._export("suppliers")),
        ], search_cb=lambda t: self._filter_suppliers(t))
        self._supplier_search = se
        b.pack_start(tb, False, False, 0)

        self._ss = Gtk.ListStore(int, str, str, str, str, str)
        tv = self._treeview(self._ss)
        for i, n in [(1, "Name"), (2, "Contact"), (3, "Phone"), (4, "Email"), (5, "Preferred")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)
        tv.connect("row-activated", lambda *_: self._edit_supplier())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._supplier_empty = self._empty_state("No suppliers. Click Add to register one.")
        self._supplier_empty.set_no_show_all(True)
        b.pack_start(self._supplier_empty, False, False, 0)
        self._stv = tv
        return b

    # ---- STOCK TRANSACTIONS ----
    def _stock_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Refresh", "view-refresh-symbolic", self.refresh),
            ("Export", "document-save-as-symbolic", lambda: self._export_stock()),
        ], search_cb=lambda t: self._filter_stock(t))
        self._stock_search = se
        b.pack_start(tb, False, False, 0)

        self._stock_s = Gtk.ListStore(int, str, str, int, str, str)
        tv = self._treeview(self._stock_s)
        for i, n in [(1, "Item"), (2, "Type"), (3, "Qty"), (4, "Notes"), (5, "Date")]:
            r = Gtk.CellRendererText()
            if i == 3:
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)

        def _stcol(col, cell, model, itr, d):
            ttype = model.get_value(itr, 2)
            if ttype == "add":
                cell.set_property("cell-background", "#e8f5e9")
            elif ttype == "remove":
                cell.set_property("cell-background", "#fce4e4")
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r, Gtk.CellRendererText):
                    c.set_cell_data_func(r, _stcol)

        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._stock_empty = self._empty_state("No stock transactions yet. Adjust stock from the Inventory tab.")
        self._stock_empty.set_no_show_all(True)
        b.pack_start(self._stock_empty, False, False, 0)
        self._stock_tv = tv
        return b

    def _filter_stock(self, t):
        self._stock_s.clear()
        stock = getattr(self, '_all_stock', [])
        filtered = 0
        for row in stock:
            if not t or any(t in str(row.get(k, '')).lower() for k in ('item_name', 'transaction_type', 'notes')):
                self._stock_s.append([
                    row['id'], row.get('item_name', ''),
                    row.get('transaction_type', ''),
                    row.get('quantity', 0),
                    row.get('notes', ''),
                    row.get('created_at', ''),
                ])
                filtered += 1
        if hasattr(self, '_stock_empty'):
            self._stock_empty.set_visible(filtered == 0)

    def _del_stock(self):
        sel = self._stock_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        sid = self._stock_s.get_value(ti, 0)
        if confirm(self, "Delete this stock transaction?"):
            try:
                self.db.u("DELETE FROM stock_transactions WHERE id=?", (sid,))
                self.refresh()
                self._status("Stock transaction deleted")
            except DatabaseError as e:
                show_error(self, str(e))

    def _export_stock(self):
        self._export("stock_transactions")

    # ---- EXPENSES ----
    def _expense_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_border_width(6)
        tb, se = self._make_toolbar([
            ("Add", "list-add-symbolic", self._add_expense),
            ("Edit", "document-edit-symbolic", self._edit_expense),
            ("Delete", "edit-delete-symbolic", self._del_expense),
            ("Export", "document-save-as-symbolic", lambda: self._export("expenses")),
        ], search_cb=lambda t: self._filter_expenses(t))
        self._expense_search = se
        b.pack_start(tb, False, False, 0)

        self._expense_s = Gtk.ListStore(int, str, str, str, float, str, str, str)
        tv = self._treeview(self._expense_s)
        for i, n in [(1, "Vessel"), (2, "Category"), (3, "Date"), (4, "Amount"), (5, "Vendor"), (6, "Description"), (7, "Tax")]:
            r = Gtk.CellRendererText()
            if i == 4:
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            c.set_sort_column_id(i)
            tv.append_column(c)

        def _excol(col, cell, model, itr, d):
            amt = model.get_value(itr, 4)
            cell.set_property("text", "$%.2f" % amt)
        tv.get_column(3).set_cell_data_func(Gtk.CellRendererText(), _excol)

        tv.connect("row-activated", lambda *_: self._edit_expense())
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        b.pack_start(sw, True, True, 0)
        self._expense_empty = self._empty_state("No expenses recorded. Click Add to log one.")
        self._expense_empty.set_no_show_all(True)
        b.pack_start(self._expense_empty, False, False, 0)
        self._expense_tv = tv
        return b

    def _filter_expenses(self, t):
        self._expense_s.clear()
        data = getattr(self, '_all_expenses', [])
        filtered = 0
        for row in data:
            if not t or any(t in str(row.get(k, '')).lower() for k in ('vessel_name', 'category', 'vendor', 'description')):
                self._expense_s.append([
                    row['id'], row.get('vessel_name', ''), row.get('category', ''),
                    row.get('expense_date', ''), row.get('amount', 0.0),
                    row.get('vendor', ''), row.get('description', ''),
                    "Deductible" if row.get('tax_deductible') else ""
                ])
                filtered += 1
        if hasattr(self, '_expense_empty'):
            self._expense_empty.set_visible(filtered == 0)

    def _add_expense(self):
        d = ExpenseDialog(self, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.add_expense(d.get_expense())
                self.refresh()
                self._status("Expense added")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _edit_expense(self):
        sel = self._expense_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        eid = self._expense_s.get_value(ti, 0)
        row = self.db.get_expense(eid)
        if not row:
            return
        d = ExpenseDialog(self, self.db, Expense.from_dict(row))
        if d.run() == Gtk.ResponseType.OK:
            try:
                e = d.get_expense()
                e.id = eid
                self.db.update_expense(e)
                self.refresh()
                self._status("Expense updated")
            except (ValidationError, DatabaseError) as e:
                show_error(self, str(e))
        d.destroy()

    def _del_expense(self):
        sel = self._expense_tv.get_selection()
        _, ti = sel.get_selected()
        if not ti:
            return
        eid = self._expense_s.get_value(ti, 0)
        if confirm(self, "Delete this expense record?"):
            try:
                self.db.delete_expense(eid)
                self.refresh()
                self._status("Expense deleted")
            except DatabaseError as e:
                show_error(self, str(e))

    # ---- FINANCIAL REPORTS ----
    def _finance_tab(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        b.set_border_width(12)
        h = Gtk.Label(label="Financial Reports — Overview & Analytics")
        h.get_style_context().add_class("professional-header")
        b.pack_start(h, False, False, 0)

        # Summary cards
        cb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cb.set_homogeneous(True)
        self._fin_total = SummaryCard("Total Expenses", "$0", "This year", "#e74c3c")
        self._fin_budget = SummaryCard("Budget Used", "0%", "Utilization", "#3498db")
        self._fin_fuel = SummaryCard("Fuel Cost", "$0", "All time", "#f39c12")
        self._fin_count = SummaryCard("Transactions", "0", "Expense count", "#27ae60")
        for c in (self._fin_total, self._fin_budget, self._fin_fuel, self._fin_count):
            cb.pack_start(c, True, True, 0)
        b.pack_start(cb, False, False, 0)

        # Category breakdown
        cf = Gtk.Frame(label="Expense by Category")
        self._cat_store = Gtk.ListStore(str, float, int)
        tv = Gtk.TreeView(model=self._cat_store)
        for i, n in [(0, "Category"), (1, "Total"), (2, "Count")]:
            r = Gtk.CellRendererText()
            if i > 0:
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(150)
        sw.add(tv)
        cf.add(sw)
        b.pack_start(cf, True, True, 0)

        # Fuel efficiency
        ff = Gtk.Frame(label="Fuel Efficiency (NMPG)")
        self._fuel_store = Gtk.ListStore(str, float, float, float)
        tv2 = Gtk.TreeView(model=self._fuel_store)
        for i, n in [(0, "Vessel"), (1, "Gallons"), (2, "Cost"), (3, "NMPG")]:
            r = Gtk.CellRendererText()
            if i > 0:
                r.set_alignment(1.0, 0.5)
            c = Gtk.TreeViewColumn(n, r, text=i)
            c.set_resizable(True)
            tv2.append_column(c)
        sw2 = Gtk.ScrolledWindow()
        sw2.set_min_content_height(120)
        sw2.add(tv2)
        ff.add(sw2)
        b.pack_start(ff, True, True, 0)

        # Refresh button
        btn = Gtk.Button(label="Refresh Reports")
        btn.connect("clicked", lambda x: self.refresh())
        b.pack_start(btn, False, False, 0)

        self._cat_tv = tv
        self._fuel_tv = tv2
        return b

    # ========== CSS ==========
    def _apply_css(self):
        css = """
        .professional-header { background: linear-gradient(135deg, #667eea, #764ba2); color: white; font-weight: bold; padding: 12px 16px; border-radius: 6px; font-size: 15px; }
        .card-title { font-size: 10px; color: rgba(255,255,255,0.8); letter-spacing: 0.5px; }
        .card-value { font-size: 26px; font-weight: bold; color: white; }
        .card-subtitle { font-size: 10px; color: rgba(255,255,255,0.65); }
        GtkTreeView { font-size: 13px; }
        GtkTreeView row:nth-child(even) { background-color: #f8f9fa; }
        GtkTreeView row:hover { background-color: #e8f0fe; }
        GtkNotebook tab { padding: 8px 16px; font-weight: 500; }
        GtkNotebook tab:checked { background: #667eea; color: white; border-radius: 4px 4px 0 0; }
        GtkStatusbar { border-top: 1px solid #dee2e6; padding: 2px 6px; font-size: 11px; }
        .status-badge { border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: bold; }
        .empty-state { font-size: 14px; color: #6c757d; font-style: italic; padding: 40px; }
        .quick-action-btn { min-width: 140px; min-height: 36px; }
        .stock-low { background-color: #fce4e4; }
        .stock-ok { background-color: #e8f5e9; }
        """
        p = Gtk.CssProvider()
        try:
            p.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception as e:
            logger.warning("CSS failed: %s", e)

    def _restore_pos(self):
        try:
            self.move(self.config.get("window_x", 100), self.config.get("window_y", 100))
        except Exception:
            pass

    # ========== CRUD: VESSELS ==========
    def _add_vessel(self):
        d=VesselDialog(self)
        if d.run()==Gtk.ResponseType.OK:
            try: v=d.get_vessel(); self.db.add_vessel(v); self.refresh(); self._status(f"Vessel '{v.name}' added")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_vessel(self):
        sel=self._vtv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        vid=self._vs.get_value(ti,0); row=self.db.get_vessel(vid)
        if not row: return
        d=VesselDialog(self,Vessel.from_dict(row))
        if d.run()==Gtk.ResponseType.OK:
            try: v=d.get_vessel(); v.id=vid; self.db.update_vessel(v); self.refresh(); self._status(f"Vessel updated")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _del_vessel(self):
        sel=self._vtv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        name=self._vs.get_value(ti,1); vid=self._vs.get_value(ti,0)
        if confirm(self,f"Delete vessel '{name}'?","This removes all associated tasks and trips."):
            try: self.db.delete_vessel(vid); self.refresh(); self._status(f"Vessel deleted")
            except DatabaseError as e: show_error(self,str(e))

    # ========== CRUD: INVENTORY ==========
    def _add_inventory(self):
        d=InventoryDialog(self)
        if d.run()==Gtk.ResponseType.OK:
            try: data=d.get_data(); self.db.add_item(data['name'],data['part_number'],data['category'],data['current_stock'],data['minimum_stock'],data['unit_cost']); self.refresh(); self._status(f"Item added")
            except (ValidationError,ValueError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_inventory(self):
        sel=self._itv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        iid=self._is.get_value(ti,0); item=self.db.get_item(iid)
        if not item: return
        d=InventoryDialog(self,item)
        if d.run()==Gtk.ResponseType.OK:
            try: data=d.get_data(); self.db.update_item(iid,data['name'],data['part_number'],data['category'],data['current_stock'],data['minimum_stock'],data['unit_cost']); self.refresh(); self._status(f"Item updated")
            except (ValidationError,ValueError) as e: show_error(self,str(e))
        d.destroy()
    def _del_inventory(self):
        sel=self._itv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        name=self._is.get_value(ti,1); iid=self._is.get_value(ti,0)
        if confirm(self,f"Delete item '{name}'?","Stock transactions will also be removed."):
            try: self.db.delete_item(iid); self.refresh(); self._status(f"Item deleted")
            except DatabaseError as e: show_error(self,str(e))

    # ========== CRUD: TASKS ==========
    def _add_task(self):
        d=MaintenanceDialog(self,self.db)
        if d.run()==Gtk.ResponseType.OK:
            try:
                t=d.get_task()
                t.work_order=self.db.next_work_order()
                if not t.next_due:
                    t.next_due=datetime.datetime.now().strftime("%Y-%m-%d")
                self.db.add_task(t)
                self.refresh()
                self._status("Task added: %s (WO: %s)" % (t.task_name, t.work_order))
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_task(self):
        sel=self._ttv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        tid=self._ts.get_value(ti,0); task=self.db.get_task(tid)
        if not task: return
        d=MaintenanceDialog(self,self.db,MaintenanceTask.from_dict(task))
        if d.run()==Gtk.ResponseType.OK:
            try: t=d.get_task(); t.id=tid; t.work_order=task.get('work_order',''); self.db.update_task(t); self.refresh(); self._status("Task updated")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _del_task(self):
        sel=self._ttv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        name=self._ts.get_value(ti,4); tid=self._ts.get_value(ti,0)
        if confirm(self, "Delete task '%s'?" % name):
            try: self.db.delete_task(tid); self.refresh(); self._status("Task deleted")
            except DatabaseError as e: show_error(self,str(e))

    # ========== CRUD: TRIPS ==========
    def _add_trip(self):
        d=TripDialog(self,self.db)
        if d.run()==Gtk.ResponseType.OK:
            try: t=d.get_trip(); self.db.add_trip(t); self.refresh(); self._status(f"Trip added")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_trip(self):
        sel=self._trip_tv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        tid=self._trip_s.get_value(ti,0)
        trips=self.db.get_trips(); row=next((t for t in trips if t['id']==tid),None)
        if not row: return
        d=TripDialog(self,self.db,TripLog.from_dict(row))
        if d.run()==Gtk.ResponseType.OK:
            try: t=d.get_trip(); t.id=tid; self.db.update_trip(t); self.refresh(); self._status(f"Trip updated")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _del_trip(self):
        sel=self._trip_tv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        dest=self._trip_s.get_value(ti,2); tid=self._trip_s.get_value(ti,0)
        if confirm(self,f"Delete trip to '{dest}'?"):
            try: self.db.delete_trip(tid); self.refresh(); self._status(f"Trip deleted")
            except DatabaseError as e: show_error(self,str(e))

    # ========== CRUD: SUPPLIERS ==========
    def _add_supplier(self):
        d=SupplierDialog(self)
        if d.run()==Gtk.ResponseType.OK:
            try: s=d.get_supplier(); self.db.add_supplier(s); self.refresh(); self._status(f"Supplier added")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_supplier(self):
        sel=self._stv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        sid=self._ss.get_value(ti,0)
        suppliers=self.db.get_suppliers(); row=next((s for s in suppliers if s['id']==sid),None)
        if not row: return
        d=SupplierDialog(self,Supplier.from_dict(row))
        if d.run()==Gtk.ResponseType.OK:
            try: s=d.get_supplier(); s.id=sid; self.db.update_supplier(s); self.refresh(); self._status(f"Supplier updated")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _del_supplier(self):
        sel=self._stv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        name=self._ss.get_value(ti,1); sid=self._ss.get_value(ti,0)
        if confirm(self,f"Delete supplier '{name}'?"):
            try: self.db.delete_supplier(sid); self.refresh(); self._status(f"Supplier deleted")
            except DatabaseError as e: show_error(self,str(e))

    # ========== REFRESH ==========
    def refresh(self):
        self._refresh_mgr.schedule()

    def _update_views(self, vessels, items, tasks, inv_summary, task_counts, vessel_count,
                      trips, suppliers, stock, expenses, finsum, feff, crew, crew_counts, certifications, templates,
                      training=None, watch=None):
        request_id.set(str(uuid.uuid4())[:8])
        self._all_vessels = vessels
        self._all_items = items
        self._all_tasks = tasks
        self._all_trips = trips
        self._all_suppliers = suppliers
        self._all_stock = stock
        self._all_expenses = expenses
        self._all_crew = crew
        self._all_certifications = certifications
        self._all_templates = templates
        self._all_training = training or []
        self._all_watch = watch or []
        has_data = vessel_count > 0 or task_counts.get('total', 0) > 0 or inv_summary.get('total', 0) > 0

        # Show/hide welcome panel based on data presence
        if hasattr(self, '_welcome_box'):
            self._welcome_box.set_visible(not has_data)

        # Dashboard
        self._cd_v.update(str(vessel_count))
        self._cd_t.update(str(task_counts.get('due_soon', 0)))
        self._cd_s.update(str(inv_summary.get('low_count', 0)))
        self._cd_o.update(str(task_counts.get('overdue', 0)))
        self._cd_tx.update(str(len(stock)))
        inv_val = inv_summary.get('total_value', 0)
        self._cd_vl.update("$%s" % format(inv_val, ',.0f'))
        if hasattr(self, '_cd_crew'):
            self._cd_crew.update(str(crew_counts.get('active', 0)))
        if hasattr(self, '_cd_certs'):
            self._cd_certs.update(str(crew_counts.get('expiring_certs', 0)))
        if hasattr(self, '_cd_expired'):
            self._cd_expired.update(str(crew_counts.get('expired_certs', 0)))

        # Vessels
        search=getattr(self,'_vessel_search',None)
        st=search.get_text().lower() if search else ""
        self._vs.clear()
        for v in vessels:
            if not st or any(st in str(v.get(k,'')).lower() for k in('name','vessel_type','engine_make','engine_model','current_location')):
                self._vs.append([v['id'],v['name'],v.get('vessel_type',''),v.get('engine_make',''),v.get('engine_model',''),v.get('current_location','')])

        # Inventory
        search=getattr(self,'_inv_search',None)
        st=search.get_text().lower() if search else ""
        self._is.clear()
        for item in items:
            if not st or any(st in str(item.get(k,'')).lower() for k in('name','part_number','category')):
                self._is.append([item['id'],item['name'],item.get('part_number',''),item.get('category',''),item.get('current_stock',0),item.get('minimum_stock',0),item.get('unit_cost',0.0)])

        # Tasks
        search=getattr(self,'_task_search',None)
        st=search.get_text().lower() if search else ""
        self._ts.clear()
        for task in tasks:
            wo = task.get('work_order', '') or ''
            if not st or any(st in str(task.get(k,'')).lower() for k in('vessel_name','system','task_name','status','priority','work_order')):
                self._ts.append([task['id'], wo, task.get('vessel_name',''), task.get('system',''),
                                 task['task_name'], task.get('next_due',''),
                                 task.get('status',''), task.get('priority','')])

        # Trips
        search=getattr(self,'_trip_search',None)
        st=search.get_text().lower() if search else ""
        self._trip_s.clear()
        for t in trips:
            if not st or any(st in str(t.get(k,'')).lower() for k in('vessel_name','destination','departure','arrival')):
                self._trip_s.append([t['id'],t.get('vessel_name',''),t.get('destination',''),t.get('departure',''),t.get('arrival',''),t.get('distance',0.0),t.get('fuel_used',0.0)])

        # Suppliers
        search=getattr(self,'_supplier_search',None)
        st=search.get_text().lower() if search else ""
        self._ss.clear()
        for s in suppliers:
            if not st or any(st in str(s.get(k,'')).lower() for k in('name','contact','phone','email')):
                self._ss.append([s['id'],s['name'],s.get('contact',''),s.get('phone',''),s.get('email',''),"Yes" if s.get('preferred') else "No"])

        # Stock transactions
        search = getattr(self, '_stock_search', None)
        st = search.get_text().lower() if search else ""
        self._stock_s.clear()
        for row in stock:
            if not st or any(st in str(row.get(k, '')).lower() for k in ('item_name', 'transaction_type', 'notes')):
                self._stock_s.append([
                    row['id'], row.get('item_name', ''),
                    row.get('transaction_type', ''),
                    row.get('quantity', 0),
                    row.get('notes', ''),
                    row.get('created_at', ''),
                ])

        # Expenses
        search = getattr(self, '_expense_search', None)
        st = search.get_text().lower() if search else ""
        self._expense_s.clear()
        for row in expenses:
            if not st or any(st in str(row.get(k, '')).lower() for k in ('vessel_name', 'category', 'vendor', 'description')):
                self._expense_s.append([
                    row['id'], row.get('vessel_name', ''), row.get('category', ''),
                    row.get('expense_date', ''), row.get('amount', 0.0),
                    row.get('vendor', ''), row.get('description', ''),
                    "Deductible" if row.get('tax_deductible') else ""
                ])
        if hasattr(self, '_expense_empty'):
            self._expense_empty.set_visible(len(self._expense_s) == 0)

        # Crew list
        search = getattr(self, '_crew_search', None)
        st = search.get_text().lower() if search else ""
        self._crew_s.clear()
        for row in crew:
            if not st or any(st in str(row.get(k, '')).lower() for k in ('first_name', 'last_name', 'role', 'email')):
                full = "%s %s" % (row.get('first_name', ''), row.get('last_name', ''))
                self._crew_s.append([
                    row['id'], full, row.get('role', ''), row.get('status', ''),
                    row.get('phone', ''), row.get('email', ''), row.get('date_joined', '')
                ])
        if hasattr(self, '_crew_empty'):
            self._crew_empty.set_visible(len(self._crew_s) == 0)

        # Training Records
        search = getattr(self, '_train_search', None)
        st = search.get_text().lower() if search else ""
        self._train_s.clear()
        for row in (training or []):
            if not st or any(st in str(row.get(k, '')).lower() for k in ('course_name', 'provider', 'crew_member')):
                self._train_s.append([
                    row['id'], row.get('crew_member', ''), row.get('course_name', ''),
                    row.get('provider', ''), row.get('date_completed', ''),
                    row.get('expiry_date', ''), row.get('cost', 0.0)
                ])
        if hasattr(self, '_train_empty'):
            self._train_empty.set_visible(len(self._train_s) == 0)

        # Watch Schedules
        search = getattr(self, '_watch_search', None)
        st = search.get_text().lower() if search else ""
        self._watch_s.clear()
        for row in (watch or []):
            if not st or any(st in str(row.get(k, '')).lower() for k in ('crew_name', 'vessel_name', 'role_on_watch')):
                self._watch_s.append([
                    row['id'], row.get('crew_name', ''), row.get('vessel_name', ''),
                    row.get('date', ''), row.get('start_time', ''),
                    row.get('end_time', ''), row.get('role_on_watch', '')
                ])
        if hasattr(self, '_watch_empty'):
            self._watch_empty.set_visible(len(self._watch_s) == 0)

        # Financial reports
        if finsum:
            self._fin_total.update("$%s" % format(finsum.get('total_expenses', 0), ',.2f'))
            self._fin_budget.update("%.1f%%" % finsum.get('budget_utilization_pct', 0))
            self._fin_fuel.update("$%s" % format(finsum.get('total_fuel_cost', 0), ',.2f'))
            self._fin_count.update(str(finsum.get('expense_count', 0)))
            # Category breakdown
            self._cat_store.clear()
            for cat in finsum.get('by_category', []):
                self._cat_store.append([cat['category'], cat['total'], cat['count']])
            # Fuel efficiency
            self._fuel_store.clear()
            for row in feff:
                self._fuel_store.append([
                    row.get('vessel_name', 'Unknown'),
                    row.get('total_gallons', 0.0),
                    row.get('total_fuel_cost', 0.0) or 0.0,
                    round(row.get('nmpg', 0.0), 2)
                ])

        # Recent activity
        self._rv.clear()
        now = datetime.datetime.now().strftime("%H:%M")
        if vessel_count:
            self._rv.append([now, "Vessels", "%d vessels registered" % vessel_count])
        if task_counts.get('overdue', 0):
            self._rv.append([now, "Tasks", "%d overdue tasks" % task_counts['overdue']])
        if inv_summary.get('low_count', 0):
            self._rv.append([now, "Inventory", "%d low-stock items" % inv_summary['low_count']])
        if crew_counts.get('active', 0):
            self._rv.append([now, "Crew", "%d active crew members" % crew_counts['active']])
        if crew_counts.get('expiring_certs', 0):
            self._rv.append([now, "Certs", "%d certs expiring in 30 days" % crew_counts['expiring_certs']])

        total_exp = finsum.get('total_expenses', 0) if finsum else 0
        self._status(
            "%d vessels | %d tasks (%d overdue) | %d items ($%s) | %d trips | %d crew | %d expenses ($%s)"
            % (vessel_count, task_counts.get('total', 0), task_counts.get('overdue', 0),
               inv_summary.get('total', 0), format(inv_val, ',.0f'),
               len(trips), crew_counts.get('active', 0), len(expenses), format(total_exp, ',.2f'))
            )

    def _status(self,msg):
        def _upd():
            if self._sb: self._sb.pop(self._sbc); self._sb.push(self._sbc,msg)
        self.ui.queue(_upd)

    # ========== EXPORT ==========
    def _export(self,entity):
        dlg=Gtk.FileChooserDialog(title=f"Export {entity}",transient_for=self,action=Gtk.FileChooserAction.SAVE)
        dlg.add_button("Cancel",Gtk.ResponseType.CANCEL); dlg.add_button("Save",Gtk.ResponseType.OK)
        dlg.set_current_name(f"{entity}_{datetime.datetime.now().strftime('%Y%m%d')}.csv")
        if dlg.run()==Gtk.ResponseType.OK:
            path=dlg.get_filename()
            try:
                n=self.db.export_csv(path,EXPORT_QUERIES[entity])
                self._status(f"Exported {n} {entity} to {os.path.basename(path)}")
            except Exception as e: show_error(self,str(e))
        dlg.destroy()

    # ========== BACKUP ==========
    def _manual_backup(self):
        try:
            d = Path(BACKUP_DIR)
            d.mkdir(exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            bp = d / "manual_%s.db" % ts
            if self.db.create_backup(str(bp)):
                self._status("Backup: %s" % bp)
            else:
                self._status("Backup failed")
        except Exception as e:
            show_error(self, "Backup failed: %s" % e)

    # ========== BACKGROUND ==========
    def _start_bg(self):
        ev = threading.Event()
        self._bg_event = ev

        def backup():
            while not self._shutting:
                try:
                    now = datetime.datetime.now()
                    next_run = now.replace(hour=2, minute=0, second=0) + datetime.timedelta(days=1)
                    wait = min((next_run - now).total_seconds(), 3600)
                    if ev.wait(timeout=wait):
                        break
                    if not self._shutting and self.config.get("auto_backup", True):
                        self._auto_backup()
                except Exception:
                    try:
                        ev.wait(300)
                    except Exception:
                        break

        t = threading.Thread(target=backup, daemon=True)
        t.start()

        def cleanup():
            while not self._shutting:
                try:
                    if ev.wait(timeout=3600):
                        break
                    if not self._shutting:
                        self._cleanup_backups()
                except Exception:
                    break

        t2 = threading.Thread(target=cleanup, daemon=True)
        t2.start()

    def _auto_backup(self):
        try:
            d = Path(BACKUP_DIR)
            d.mkdir(exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            bp = d / "vessel_keeper_backup_%s.db" % ts
            if self.db.create_backup(str(bp)):
                self._cleanup_backups()
        except Exception as e:
            logger.error("Auto backup failed: %s", e)

    def _cleanup_backups(self):
        retention = self.config.get("backup_retention_days", 30)
        cutoff = time.time() - retention * 86400
        d = Path(BACKUP_DIR)
        for f in d.glob("vessel_keeper_backup_*.db"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        ed = d / "emergency"
        if ed.exists():
            for f in ed.glob("emergency_*.db"):
                try:
                    if f.stat().st_mtime < time.time() - 7 * 86400:
                        f.unlink()
                except OSError:
                    pass

    # ========== SHUTDOWN ==========
    def do_delete_event(self,e):
        if not self._shut_done: self._shutdown()
        return False
    def _shutdown(self):
        if self._shut_done:
            return
        self._shut_done = True
        self._shutting = True
        self.ui.request_shutdown()
        if hasattr(self, '_bg_event'):
            self._bg_event.set()
        try:
            x, y = self.get_position()
            w, h = self.get_size()
            self.config.set("window_x", x)
            self.config.set("window_y", y)
            self.config.set("window_width", w)
            self.config.set("window_height", h)
            self.config.save()
        except Exception:
            logger.warning("Failed to save window state during shutdown")
        try:
            self.db.close()
        except Exception:
            logger.warning("Failed to close database during shutdown")
        logger.info("Shutdown complete")
        Gtk.main_quit()

# =============================================================================
# UTILITIES
# =============================================================================
def show_error(parent,msg):
    def _show():
        d=Gtk.MessageDialog(transient_for=parent,flags=0,message_type=Gtk.MessageType.ERROR,buttons=Gtk.ButtonsType.OK,text="Error")
        d.format_secondary_text(msg); d.run(); d.destroy()
    if parent and hasattr(parent,'ui') and not parent._shutting: parent.ui.queue(_show)
    elif not parent: GLib.idle_add(_show)

def confirm(parent,msg,secondary=""):
    d=Gtk.MessageDialog(transient_for=parent,flags=0,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.YES_NO,text=msg)
    if secondary: d.format_secondary_text(secondary)
    r=d.run(); d.destroy(); return r==Gtk.ResponseType.YES

def setup_excepthook():
    def handler(typ,val,tb):
        if issubclass(typ,KeyboardInterrupt): sys.__excepthook__(typ,val,tb); return
        logger.critical("Uncaught", exc_info=(typ, val, tb))
        show_error(None, "Error: %s" % val)
    sys.excepthook=handler

def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    for d in [DATA_DIR, REPORTS_DIR, BACKUP_DIR, LOG_DIR, os.path.join(BACKUP_DIR, "emergency")]:
        os.makedirs(d, exist_ok=True)
    setup_excepthook()
    app = None
    try:
        app = VesselKeeperApp()
        app.connect("destroy", Gtk.main_quit)
        app.show_all()
        logger.info("Started")
        Gtk.main()
    except Exception as e:
        logging.critical("Startup failed: %s", e, exc_info=True)
        show_error(None, "Failed: %s" % e)
        sys.exit(1)
    finally:
        if app and not app._shut_done:
            app._shutdown()

if __name__ == "__main__":
    main()
