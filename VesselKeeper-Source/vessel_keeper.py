#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib
import sys, os, sqlite3, logging, json, csv, datetime, threading, signal
import shutil, contextvars, uuid, hashlib, hmac
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Iterator
from contextlib import contextmanager
from queue import Queue, Empty, Full
import time
import zipfile
import getpass
import platform
from pathlib import Path
try:
    from logging.handlers import RotatingFileHandler
except Exception:
    RotatingFileHandler = None
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

# The jurisdiction rulesets live next to this script. Ensure imports work when
# the application is launched from another working directory or via a symlink.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from jurisdictions import (
        DEFAULT_JURISDICTION,
        get_registry,
        imo_number_valid,
        reload_registry,
    )
    JURISDICTIONS_AVAILABLE = True
except Exception as _jurisdiction_error:  # pragma: no cover - defensive
    DEFAULT_JURISDICTION = "tt"
    JURISDICTIONS_AVAILABLE = False
    _JURISDICTION_IMPORT_ERROR = _jurisdiction_error

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "vessel_keeper")
DATA_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "vessel_keeper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DATABASE_FILE = os.path.join(DATA_DIR, "vessel_keeper.db")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
RECEIPTS_DIR = os.path.join(DATA_DIR, "receipts")
LOG_DIR = os.path.join(DATA_DIR, "logs")
MAX_SCHEMA_VERSION = 10
PIN_ITERATIONS = 120000

# Product identity. Override the build string at package time
# (VESSEL_KEEPER_BUILD) so support can identify an installed copy.
APP_NAME = "Vessel Keeper"
APP_SLUG = "vessel-keeper"
APP_ID = "com.vesselkeeper.VesselKeeper"
APP_VERSION = "1.0.0"
APP_BUILD = os.environ.get("VESSEL_KEEPER_BUILD", "source")
APP_VENDOR = os.environ.get("VESSEL_KEEPER_VENDOR", "Vessel Keeper")
APP_WEBSITE = os.environ.get("VESSEL_KEEPER_WEBSITE", "")
APP_SUPPORT_EMAIL = os.environ.get("VESSEL_KEEPER_SUPPORT", "")
APP_COPYRIGHT = "Copyright © 2026 Vessel Keeper"
APP_LICENSE = "MIT"
APP_CONFIG_ENV = "VESSEL_KEEPER_CONFIG_DIR"
APP_DATA_ENV = "VESSEL_KEEPER_DATA_DIR"


def _product_dir(env_key, windows_folder, unix_parts):
    override = os.environ.get(env_key)
    if override:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(override)))
    if os.name == "nt":
        base = (os.environ.get(windows_folder)
                or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
        return os.path.join(base, "VesselKeeper")
    return os.path.join(os.path.expanduser("~"), *unix_parts)


# Resolve user directories per platform. Explicit environment overrides keep
# portable installs and the test-suite isolated from a real profile.
CONFIG_DIR = _product_dir(APP_CONFIG_ENV, "APPDATA", (".config", "vessel_keeper"))
DATA_DIR = _product_dir(APP_DATA_ENV, "LOCALAPPDATA",
                        (".local", "share", "vessel_keeper"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DATABASE_FILE = os.path.join(DATA_DIR, "vessel_keeper.db")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
RECEIPTS_DIR = os.path.join(DATA_DIR, "receipts")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# Default jurisdiction for every installation. Trinidad and Tobago is the base
# preset; users can switch to any CARICOM nation in Preferences. All values are
# advisory and can be overridden.
DEFAULT_COUNTRY_CODE = "TT"
DEFAULT_LOCALE = "en_TT"
DEFAULT_TIMEZONE = "America/Port_of_Spain"
DEFAULT_DATE_FORMAT = "DD/MM/YYYY"
DEFAULT_CURRENCY = "TTD"
DEFAULT_TAX_REGIME = "VAT"
DEFAULT_TAX_RATE = 12.5

CURRENCY_SYMBOLS = {
    "USD": "$", "TTD": "TT$", "BBD": "Bds$", "JMD": "J$", "XCD": "EC$",
    "BZD": "BZ$", "GYD": "G$", "HTG": "G", "SRD": "SRD ", "BSD": "B$",
    "BMD": "BD$", "KYD": "CI$", "EUR": "€", "GBP": "£", "CAD": "CA$",
    "AUD": "A$", "JPY": "¥",
}
DATE_FORMAT_PATTERNS = {
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD-MM-YYYY": "%d-%m-%Y",
    "YYYY/MM/DD": "%Y/%m/%d",
    "DD.MM.YYYY": "%d.%m.%Y",
}
_format_state = {"currency": DEFAULT_CURRENCY, "date_format": DEFAULT_DATE_FORMAT}


def set_format_state(currency=None, date_format=None):
    """Update the process-wide display formatting used by tables and dialogs."""
    if currency:
        _format_state["currency"] = str(currency).strip().upper() or DEFAULT_CURRENCY
    if date_format in DATE_FORMAT_PATTERNS:
        _format_state["date_format"] = date_format


def get_format_state():
    return dict(_format_state)


def currency_symbol(currency=None):
    code = str(currency or _format_state["currency"]).strip().upper()
    return CURRENCY_SYMBOLS.get(code, (code + " ") if code else "")


def format_money(value, currency=None):
    """Format a number using the active currency symbol and thousands grouping."""
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    symbol = currency_symbol(currency)
    text = "%s%s" % (symbol, "{:,.2f}".format(abs(amount)))
    return ("-" + text) if amount < 0 else text


def format_date(value):
    """Display an internal ISO date using the active jurisdiction format."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return text
    pattern = DATE_FORMAT_PATTERNS.get(_format_state["date_format"], "%d/%m/%Y")
    return parsed.strftime(pattern)


def format_datetime(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.datetime.strptime(text, fmt)
            break
        except (ValueError, TypeError):
            continue
    if parsed is None:
        return format_date(text) if len(text) >= 10 else text
    return "%s %s" % (format_date(parsed.date().isoformat()), parsed.strftime("%H:%M"))


def date_input_hint():
    return _format_state["date_format"]


def tax_preset_label(ruleset, rate=None, name=None):
    """Human-readable tax preset label; avoids implying a zero-rate tax."""
    name = str(name or getattr(ruleset, "tax_name", "Tax") or "Tax")
    try:
        value = float(rate if rate is not None
                      else getattr(ruleset, "tax_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return "%s %.2f%%" % (name, value)
    return "no consumption tax preset"


def parse_date_input(value):
    """Parse a user-entered date into ISO, accepting ISO and local formats."""
    text = str(value or "").strip()
    if not text:
        return ""
    patterns = [
        "%Y-%m-%d",
        DATE_FORMAT_PATTERNS.get(_format_state["date_format"], "%d/%m/%Y"),
        "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
    ]
    seen = set()
    for pattern in patterns:
        if pattern in seen:
            continue
        seen.add(pattern)
        try:
            return datetime.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(text)


def _load_jurisdiction_registry(allow_custom=True):
    """Return a registry, or None when the ruleset package is unavailable."""
    if not JURISDICTIONS_AVAILABLE:
        return None
    try:
        return get_registry(allow_custom=allow_custom)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not load jurisdiction rulesets: %s", e)
        return None

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

# Set when the user has asked the application to exit. Background threads and
# the exception handlers stop writing to stdout/stderr once this is set: a
# daemon thread that logs while Python is finalizing can deadlock or abort the
# interpreter ("_enter_buffered_busy: could not acquire lock for BufferedWriter").
_EXITING = threading.Event()


class StructuredLogger:
    def __init__(self):
        self.logger = logging.getLogger("vessel_keeper")
        handlers = []
        try:
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            log_path = os.path.join(LOG_DIR, "vessel_keeper.log")
            if RotatingFileHandler is not None:
                handlers.append(RotatingFileHandler(log_path,
                                                    maxBytes=5 * 1024 * 1024,
                                                    backupCount=5,
                                                    encoding='utf-8'))
            else:
                handlers.append(logging.FileHandler(log_path, encoding='utf-8'))
        except OSError as e:
            # Never prevent the application from starting just because the log
            # directory or file is not writable.
            print("Warning: could not open log file: %s" % e, file=sys.stderr)
        handlers.append(logging.StreamHandler(sys.stdout))
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO,
                format='%(asctime)s | %(name)-20s | %(levelname)-8s | %(threadName)-15s | %(message)s',
                handlers=handlers)
        else:
            # Attach handlers if not already attached
            root = logging.getLogger()
            for h in handlers:
                if not any(type(x) == type(h) and getattr(x, 'baseFilename', None) == getattr(h, 'baseFilename', None) for x in root.handlers):
                    root.addHandler(h)

    def _log(self, level, msg, *args, exc_info=None, **ctx):
        # Never log from a background thread once shutdown has begun.
        if _EXITING.is_set():
            return
        if args:
            msg = msg % args
        getattr(self.logger, level)("%s | request_id=%s%s", msg, request_id.get(), self._fmt(ctx), exc_info=exc_info)

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
# DATACLASSES, VALIDATION, SECURITY HELPERS
# =============================================================================
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
    # Jurisdiction / registration details
    flag_state: str = ""
    port_of_registry: str = ""
    official_number: str = ""
    imo_number: str = ""
    registration_expiry: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), name=d.get('name', ''), vessel_type=d.get('vessel_type', ''),
                   engine_make=d.get('engine_make', ''), engine_model=d.get('engine_model', ''),
                   current_location=d.get('current_location', ''),
                   engine_hours=d.get('engine_hours', 0.0) or 0.0,
                   last_hours_update=d.get('last_hours_update', ''),
                   flag_state=d.get('flag_state', '') or '',
                   port_of_registry=d.get('port_of_registry', '') or '',
                   official_number=d.get('official_number', '') or '',
                   imo_number=d.get('imo_number', '') or '',
                   registration_expiry=d.get('registration_expiry', '') or '',
                   created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0), vessel_name=d.get('vessel_name', ''),
                   system=d.get('system', ''), task_name=d.get('task_name', ''),
                   description=d.get('description', ''), next_due=d.get('next_due', ''),
                   status=d.get('status', 'pending'), priority=d.get('priority', 'medium'),
                   completed_at=d.get('completed_at', ''), completed_by=d.get('completed_by', ''),
                   interval_days=d.get('interval_days'), interval_hours=d.get('interval_hours'),
                   is_recurring=bool(d.get('is_recurring', 0)), work_order=d.get('work_order', ''),
                   completed_notes=d.get('completed_notes', ''), cost=d.get('cost', 0.0) or 0.0,
                   created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0), vessel_name=d.get('vessel_name', ''),
                   departure=d.get('departure', ''), arrival=d.get('arrival', ''),
                   destination=d.get('destination', ''), distance=d.get('distance', 0.0) or 0.0,
                   fuel_used=d.get('fuel_used', 0.0) or 0.0, created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), name=d.get('name', ''), contact=d.get('contact', ''),
                   phone=d.get('phone', ''), email=d.get('email', ''),
                   preferred=bool(d.get('preferred', 0)), created_at=d.get('created_at', ''))


@dataclass
class Expense:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    trip_id: Optional[int] = None
    category: str = ""
    amount: float = 0.0
    currency: str = DEFAULT_CURRENCY
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
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0) or 0, vessel_name=d.get('vessel_name', ''),
                   trip_id=d.get('trip_id'), category=d.get('category', ''), amount=d.get('amount', 0.0) or 0.0,
                   currency=d.get('currency', DEFAULT_CURRENCY) or DEFAULT_CURRENCY, expense_date=d.get('expense_date', ''),
                   description=d.get('description', ''), vendor=d.get('vendor', ''),
                   receipt_path=d.get('receipt_path', ''), tax_deductible=bool(d.get('tax_deductible', 0)),
                   tax_category=d.get('tax_category', ''), gst_rate=d.get('gst_rate', 0.0) or 0.0,
                   gst_amount=d.get('gst_amount', 0.0) or 0.0, created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id', 0) or 0, vessel_name=d.get('vessel_name', ''),
                   fiscal_year=d.get('fiscal_year', 0) or 0, category=d.get('category', ''),
                   budget_amount=d.get('budget_amount', 0.0) or 0.0,
                   spent_amount=d.get('spent_amount', 0.0) or 0.0,
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
        return cls(id=d.get('id'), trip_id=d.get('trip_id'), vessel_id=d.get('vessel_id', 0) or 0,
                   vessel_name=d.get('vessel_name', ''), gallons=d.get('gallons', 0.0) or 0.0,
                   cost_per_gallon=d.get('cost_per_gallon', 0.0) or 0.0,
                   total_cost=d.get('total_cost', 0.0) or 0.0,
                   fuel_type=d.get('fuel_type', ''), vendor=d.get('vendor', ''),
                   location=d.get('location', ''), engine_hours=d.get('engine_hours', 0.0) or 0.0,
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
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0) or 0, name=d.get('name', ''),
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
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0) or 0,
                   course_name=d.get('course_name', ''), provider=d.get('provider', ''),
                   date_completed=d.get('date_completed', ''), expiry_date=d.get('expiry_date', ''),
                   cost=d.get('cost', 0.0) or 0.0, notes=d.get('notes', ''),
                   created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), crew_member_id=d.get('crew_member_id', 0) or 0,
                   vessel_id=d.get('vessel_id', 0) or 0, date=d.get('date', ''),
                   start_time=d.get('start_time', ''), end_time=d.get('end_time', ''),
                   role_on_watch=d.get('role_on_watch', ''), notes=d.get('notes', ''),
                   created_at=d.get('created_at', ''))


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
        return cls(id=d.get('id'), task_id=d.get('task_id', 0) or 0, item_id=d.get('item_id', 0) or 0,
                   quantity_used=d.get('quantity_used', 1) or 1, cost_per_unit=d.get('cost_per_unit', 0.0) or 0.0,
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


class DatabaseError(Exception):
    pass


class ValidationError(Exception):
    pass


class ResourceError(Exception):
    pass


class SecurityError(Exception):
    pass


def hash_pin(pin: str) -> str:
    """PBKDF2-HMAC-SHA256 PIN hash. Format: pbkdf2_sha256$iterations$salt$digest."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PIN_ITERATIONS, salt.hex(), dk.hex())


def verify_pin(pin: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


class DataValidator:
    @staticmethod
    def required(val, field):
        stripped = (val or "").strip()
        if not stripped:
            raise ValidationError(f"{field} cannot be empty")
        return stripped

    @staticmethod
    def maxlen(val, field, mx=255):
        if len(val or "") > mx:
            raise ValidationError(f"{field} too long (max {mx})")
        return val

    @staticmethod
    def to_float(val, field, default=0.0):
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValidationError("%s must be a number" % field)

    @staticmethod
    def to_int(val, field, default=0):
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        try:
            return int(float(val))
        except (TypeError, ValueError):
            raise ValidationError("%s must be a whole number" % field)

    @staticmethod
    def date_optional(val, field):
        s = (val or "").strip()
        if not s:
            return s
        try:
            return parse_date_input(s)
        except ValueError:
            raise ValidationError("%s must be a valid date (for example %s or YYYY-MM-DD)"
                                  % (field, date_input_hint()))

    @staticmethod
    def time_optional(val, field):
        s = (val or "").strip()
        if not s:
            return s
        for pattern in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.datetime.strptime(s, pattern).strftime("%H:%M")
            except ValueError:
                continue
        raise ValidationError("%s must be in HH:MM format" % field)

    @staticmethod
    def validate_vessel(v):
        DataValidator.required(v.name, "Vessel name")
        DataValidator.maxlen(v.name, "Vessel name")
        DataValidator.maxlen(v.vessel_type, "Vessel type", 100)
        DataValidator.maxlen(v.engine_make, "Engine make", 100)
        DataValidator.maxlen(v.engine_model, "Engine model", 100)
        DataValidator.maxlen(v.flag_state, "Flag state", 100)
        DataValidator.maxlen(v.port_of_registry, "Port of registry", 120)
        DataValidator.maxlen(v.official_number, "Official number", 60)
        DataValidator.maxlen(v.imo_number, "IMO number", 20)
        if (v.engine_hours or 0) < 0:
            raise ValidationError("Engine hours cannot be negative")
        v.registration_expiry = DataValidator.date_optional(
            v.registration_expiry, "Registration expiry")
        imo = (v.imo_number or "").strip()
        if imo:
            v.imo_number = imo
            if not imo.isdigit() or len(imo) != 7:
                raise ValidationError("IMO number must be exactly 7 digits")
            if JURISDICTIONS_AVAILABLE and not imo_number_valid(imo):
                raise ValidationError(
                    "IMO number fails the check-digit test (verify the seventh digit)")

    @staticmethod
    def validate_task(t):
        if t.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        DataValidator.required(t.task_name, "Task name")
        DataValidator.maxlen(t.task_name, "Task name")
        if t.status not in ('pending', 'in_progress', 'completed', 'overdue'):
            raise ValidationError(f"Invalid status: {t.status}")
        if t.priority not in ('low', 'medium', 'high', 'critical'):
            raise ValidationError(f"Invalid priority: {t.priority}")
        if t.interval_days is not None and t.interval_days <= 0:
            raise ValidationError("Interval (days) must be positive")
        if t.interval_hours is not None and t.interval_hours <= 0:
            raise ValidationError("Interval (hours) must be positive")
        if (t.cost or 0) < 0:
            raise ValidationError("Cost cannot be negative")
        t.next_due = DataValidator.date_optional(t.next_due, "Due date")

    @staticmethod
    def validate_stock(cur, mini):
        if cur < 0:
            raise ValidationError("Stock cannot be negative")
        if mini < 0:
            raise ValidationError("Minimum stock cannot be negative")

    @staticmethod
    def validate_item_name(n):
        s = (n or "").strip()
        if not s:
            raise ValidationError("Item name cannot be empty")
        return s

    @staticmethod
    def validate_supplier(s):
        DataValidator.required(s.name, "Supplier name")
        DataValidator.maxlen(s.name, "Supplier name")

    @staticmethod
    def validate_crew_member(c):
        DataValidator.required(c.first_name, "First name")
        DataValidator.maxlen(c.first_name, "First name", 100)
        DataValidator.required(c.last_name, "Last name")
        DataValidator.maxlen(c.last_name, "Last name", 100)
        DataValidator.required(c.role, "Role")
        DataValidator.maxlen(c.role, "Role", 100)
        if c.status not in ('active', 'inactive', 'terminated'):
            raise ValidationError("Invalid crew status")
        c.date_joined = DataValidator.date_optional(c.date_joined, "Date joined")
        c.date_left = DataValidator.date_optional(c.date_left, "Date left")
        if c.date_joined and c.date_left and c.date_left < c.date_joined:
            raise ValidationError("Date left cannot be before date joined")

    @staticmethod
    def validate_certification(c):
        DataValidator.required(c.name, "Certification name")
        DataValidator.maxlen(c.name, "Certification name")
        if c.crew_member_id <= 0:
            raise ValidationError("Select a crew member")
        c.issue_date = DataValidator.date_optional(c.issue_date, "Issue date")
        c.expiry_date = DataValidator.date_optional(c.expiry_date, "Expiry date")
        if c.issue_date and c.expiry_date and c.expiry_date < c.issue_date:
            raise ValidationError("Expiry date cannot be before issue date")

    @staticmethod
    def validate_training(t):
        DataValidator.required(t.course_name, "Course name")
        DataValidator.maxlen(t.course_name, "Course name")
        if t.crew_member_id <= 0:
            raise ValidationError("Select a crew member")
        if (t.cost or 0) < 0:
            raise ValidationError("Cost cannot be negative")
        t.date_completed = DataValidator.date_optional(t.date_completed, "Date completed")
        t.expiry_date = DataValidator.date_optional(t.expiry_date, "Expiry date")
        if t.date_completed and t.expiry_date and t.expiry_date < t.date_completed:
            raise ValidationError("Expiry date cannot be before completion date")

    @staticmethod
    def validate_watch_schedule(w):
        if w.crew_member_id <= 0:
            raise ValidationError("Select a crew member")
        DataValidator.required(w.date, "Date")
        w.date = DataValidator.date_optional(w.date, "Date")
        DataValidator.required(w.start_time, "Start time")
        DataValidator.required(w.end_time, "End time")
        w.start_time = DataValidator.time_optional(w.start_time, "Start time")
        w.end_time = DataValidator.time_optional(w.end_time, "End time")

    @staticmethod
    def validate_task_part(tp):
        if tp.task_id <= 0:
            raise ValidationError("Select a task")
        if tp.item_id <= 0:
            raise ValidationError("Select an inventory item")
        tp.quantity_used = DataValidator.to_int(tp.quantity_used, "Quantity", 1)
        if tp.quantity_used < 1:
            raise ValidationError("Quantity must be at least 1")
        tp.cost_per_unit = DataValidator.to_float(tp.cost_per_unit or 0.0, "Cost per unit")
        if tp.cost_per_unit < 0:
            raise ValidationError("Cost per unit cannot be negative")

    @staticmethod
    def validate_template(t):
        DataValidator.required(t.name, "Template name")
        DataValidator.maxlen(t.name, "Template name")
        if t.interval_days is not None and t.interval_days <= 0:
            raise ValidationError("Interval (days) must be positive")
        if t.interval_hours is not None and t.interval_hours <= 0:
            raise ValidationError("Interval (hours) must be positive")

# =============================================================================
# CONFIGURATION
# =============================================================================
class AppConfig:
    def __init__(self):
        self.window_x = 100
        self.window_y = 100
        self.window_width = 1200
        self.window_height = 800
        self.due_soon_days = 7
        self.backup_retention_days = 30
        self.auto_backup = True
        self.backup_interval_hours = 24
        # Security / compliance
        self.require_pin = False
        self.pin_hash = ""
        self.session_timeout_minutes = 30
        self.audit_retention_days = 365
        self.data_retention_days = 730
        self.prune_old_data = False
        # Financial / company
        self.company_name = ""
        # Caribbean-friendly defaults: Trinidad and Tobago, TTD, en_TT and VAT.
        # Every value is a preset and can be changed in Preferences.
        self.country = DEFAULT_COUNTRY_CODE
        self.locale = DEFAULT_LOCALE
        self.timezone = DEFAULT_TIMEZONE
        self.date_format = DEFAULT_DATE_FORMAT
        self.currency = DEFAULT_CURRENCY
        self.tax_regime = DEFAULT_TAX_REGIME
        self.tax_rate = DEFAULT_TAX_RATE
        self.gst_rate = DEFAULT_TAX_RATE
        self.fiscal_year_start = "01-01"
        self.jurisdiction_preset = DEFAULT_JURISDICTION
        self.jurisdiction_ruleset_id = DEFAULT_JURISDICTION
        self.jurisdiction_ruleset_version = "1.0.0"
        self.jurisdiction_ruleset_updated_at = ""
        self.allow_custom_rulesets = True
        self.show_guardrails = True
        self.verify_tax_rate = True

    def items(self):
        return ((k, getattr(self, k)) for k in dir(self) if not k.startswith('_') and not callable(getattr(self, k)))


class ConfigManager:
    def __init__(self, cf=CONFIG_FILE):
        self.config_file = cf
        self._config = AppConfig()
        self._lock = threading.RLock()
        self._loaded_keys = set()
        self._load_config()
        self._ensure_jurisdiction_defaults()

    def _load_config(self):
        try:
            with open(self.config_file) as f:
                uc = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Config load failed: %s", e)
            return
        if not isinstance(uc, dict):
            logger.warning("Config file does not contain an object; ignoring it")
            return
        defaults = AppConfig()
        self._loaded_keys = set(uc.keys())
        with self._lock:
            for k, v in uc.items():
                if not hasattr(defaults, k):
                    continue
                try:
                    v = self._coerce(getattr(defaults, k), v, k)
                except (TypeError, ValueError) as e:
                    logger.warning("Ignoring invalid config value for %s (%r): %s", k, v, e)
                    continue
                setattr(self._config, k, v)

    def _ensure_jurisdiction_defaults(self):
        """Apply the active (or default) ruleset to keys the user has not set.

        This makes the Caribbean guard rails the default for every install,
        while explicit user preferences always win.
        """
        registry = _load_jurisdiction_registry(
            allow_custom=bool(getattr(self._config, "allow_custom_rulesets", True)))
        if registry is None:
            return
        requested = getattr(self._config, "jurisdiction_preset", "") or DEFAULT_JURISDICTION
        ruleset = registry.get(requested) or registry.default()
        if ruleset is None:
            return
        defaults = ruleset.get_defaults()
        with self._lock:
            for key, value in defaults.items():
                if key in self._loaded_keys:
                    continue
                if hasattr(self._config, key):
                    setattr(self._config, key, value)
            # Keep the legacy gst_rate field and the new tax_rate field aligned.
            if "gst_rate" in self._loaded_keys:
                try:
                    self._config.tax_rate = float(self._config.gst_rate)
                except (TypeError, ValueError):
                    self._config.tax_rate = 0.0
            elif "tax_rate" in self._loaded_keys:
                try:
                    self._config.gst_rate = float(self._config.tax_rate)
                except (TypeError, ValueError):
                    self._config.gst_rate = 0.0
            else:
                self._config.gst_rate = float(defaults.get("tax_rate", 0.0))
            if "currency" not in self._loaded_keys:
                self._config.currency = ruleset.currency
            if "locale" not in self._loaded_keys:
                self._config.locale = ruleset.locale
            if "date_format" not in self._loaded_keys:
                self._config.date_format = ruleset.date_format
            if "country" not in self._loaded_keys:
                self._config.country = ruleset.country_code
            if "timezone" not in self._loaded_keys:
                self._config.timezone = ruleset.timezone
            self._config.jurisdiction_ruleset_id = ruleset.id
            self._config.jurisdiction_ruleset_version = ruleset.ruleset_version
            self._config.jurisdiction_ruleset_updated_at = ruleset.updated_at

    def apply_jurisdiction_defaults(self, ruleset_id):
        """Overwrite jurisdiction-related settings with a ruleset's presets."""
        registry = _load_jurisdiction_registry(
            allow_custom=bool(getattr(self._config, "allow_custom_rulesets", True)))
        if registry is None:
            return False
        ruleset = registry.get(ruleset_id)
        if ruleset is None:
            return False
        defaults = ruleset.get_defaults()
        with self._lock:
            for key, value in defaults.items():
                if hasattr(self._config, key):
                    setattr(self._config, key, value)
            self._config.gst_rate = float(defaults.get("tax_rate", 0.0))
            self._config.jurisdiction_preset = ruleset.id
            self._config.jurisdiction_ruleset_id = ruleset.id
            self._config.jurisdiction_ruleset_version = ruleset.ruleset_version
            self._config.jurisdiction_ruleset_updated_at = ruleset.updated_at
        set_format_state(self._config.currency, self._config.date_format)
        return True

    @staticmethod
    def _coerce(default, value, key=""):
        """Keep hand-edited or older config files from breaking startup."""
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off", ""):
                return False
            raise ValueError("expected a boolean")
        if isinstance(default, int):
            n = int(value)
            if n < 0 and key not in ("window_x", "window_y"):
                raise ValueError("expected a non-negative integer")
            return n
        if isinstance(default, float):
            n = float(value)
            if n < 0:
                raise ValueError("expected a non-negative number")
            return n
        if isinstance(default, str):
            if value is None:
                return ""
            return str(value)
        return value

    def get(self, k, d=None):
        with self._lock:
            return getattr(self._config, k, d)

    def set(self, k, v):
        with self._lock:
            if not hasattr(self._config, k):
                logger.warning("Unknown config key ignored: %s", k)
                return False
            setattr(self._config, k, v)
            return True

    def save(self):
        try:
            target_dir = os.path.dirname(os.path.abspath(self.config_file)) or CONFIG_DIR
            os.makedirs(target_dir, exist_ok=True)
            tmp = "%s.tmp" % self.config_file
            with open(tmp, 'w') as f:
                json.dump(dict(self._config.items()), f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.config_file)
        except IOError as e:
            logger.error("Config save failed: %s", e)
            raise

# =============================================================================
# DATABASE
# =============================================================================
class DatabaseConnectionPool:
    def __init__(self, db_path, max_connections=3, timeout=30.0):
        self.db_path = db_path
        self.max_connections = max_connections
        self.timeout = timeout
        self._connections = Queue(max_connections)
        self._in_use = set()
        self._lock = threading.RLock()
        self._closed = False
        for _ in range(max_connections):
            self._connections.put(self._create_connection())

    def _create_connection(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")
            conn.execute("PRAGMA temp_store=MEMORY")
            return conn
        except sqlite3.Error as e:
            raise DatabaseError("DB connection failed: %s" % e) from e

    def _health_check(self, conn):
        try:
            conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def get_connection(self):
        if self._closed:
            raise DatabaseError("Pool closed")
        conn = None
        try:
            conn = self._acquire()
            yield conn
            conn.commit()
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            raise DatabaseError(f"DB error: {e}") from e
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                self._release(conn)

    def _acquire(self):
        deadline = time.monotonic() + self.timeout
        delay = 0.05
        failures = 0
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                conn = self._connections.get(timeout=min(delay, rem))
            except Empty:
                delay = min(delay * 1.5, 1.0)
                continue
            if self._health_check(conn):
                with self._lock:
                    if not self._closed:
                        self._in_use.add(id(conn))
                        return conn
                conn.close()
                break
            conn.close()
            failures += 1
            time.sleep(min(0.05 * failures, 0.5))
        raise DatabaseError("Timeout after %.0fs waiting for a database connection" % self.timeout)

    def _release(self, conn):
        try:
            with self._lock:
                self._in_use.discard(id(conn))
            if not self._closed and self._health_check(conn):
                try:
                    self._connections.put_nowait(conn)
                except Full:
                    conn.close()
            else:
                conn.close()
                if not self._closed:
                    try:
                        self._connections.put_nowait(self._create_connection())
                    except (Full, sqlite3.Error, DatabaseError):
                        pass
        except (Full, sqlite3.Error):
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self):
        # Never block the caller while closing: waiting for the WAL checkpoint
        # or a busy lock here used to freeze the UI for 5 seconds per pooled
        # connection (15s with the default pool) whenever another reader was
        # active. A PASSIVE checkpoint does not wait and a zero busy timeout
        # makes close() return immediately.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conns = []
            while not self._connections.empty():
                try:
                    conns.append(self._connections.get_nowait())
                except Empty:
                    break
        for c in conns:
            try:
                c.execute("PRAGMA busy_timeout=0")
            except Exception:
                pass
            try:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        with self._lock:
            self._in_use.clear()


class DatabaseMetrics:
    def __init__(self):
        self._lock = threading.RLock()
        self._q = 0
        self._e = 0
        self._t = 0.0
        self._slow = 1.0

    def record_success(self, d):
        with self._lock:
            self._q += 1
            self._t += d
        if d > self._slow:
            logger.warning("Slow query: %.3fs", d)

    def record_error(self, d):
        with self._lock:
            self._e += 1
            self._t += d

    def get_summary(self):
        with self._lock:
            avg = self._t / self._q if self._q else 0
            er = self._e / self._q * 100 if self._q else 0
            return {"total_queries": self._q, "error_count": self._e, "error_rate_pct": er,
                    "avg_query_time_s": avg, "total_query_time_s": self._t}


class ThreadSafeDatabase:
    def __init__(self, db_path: str = DATABASE_FILE):
        self.db_path = db_path
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        except OSError as e:
            raise DatabaseError("Cannot create database directory: %s" % e) from e
        self.connection_pool = DatabaseConnectionPool(db_path, max_connections=3)
        self._qlock = threading.RLock()
        self._metrics = DatabaseMetrics()
        self._vcache: Dict[int, str] = {}
        self._vlock = threading.RLock()
        self._ensure()

    # ---- backups / recovery ----
    @staticmethod
    def _validate_backup_path(path: str) -> str:
        p = Path(path).resolve()
        allowed = Path(BACKUP_DIR).resolve()
        try:
            p.relative_to(allowed)
        except ValueError:
            raise ValidationError("Backup path must be under %s" % allowed)
        if "'" in str(p):
            raise ValidationError("Invalid characters in backup path")
        return str(p)

    def _remove_sidecars(self):
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def _ensure(self):
        logger.info("DB integrity check")
        if not self._verify():
            if os.path.exists(self.db_path) and os.stat(self.db_path).st_size > 0:
                logger.warning("Database file failed header/size verification; attempting recovery")
                self._quarantine_db()
                if self._restore_backup():
                    logger.info("Database restored from a verified backup")
                else:
                    logger.warning("No usable backup found; creating a new database")
                    self._init_new()
            else:
                self._init_new()
            # A restored backup may predate the current schema, so always bring
            # the recovered database up to date before using it.
            self._migrate()
            self.prune_old_data()
            return
        if not self._integrity_check()["healthy"]:
            self._handle_corruption()
        self._migrate()
        self.prune_old_data()

    def _verify(self):
        if not os.path.exists(self.db_path):
            return False
        try:
            if os.stat(self.db_path).st_size == 0:
                return False
            with open(self.db_path, 'rb') as f:
                return f.read(16).startswith(b'SQLite format 3')
        except (OSError, IOError):
            return False

    def _integrity_check(self):
        r = {"healthy": False, "errors": []}
        try:
            with self.connection_pool.get_connection() as conn:
                q = conn.execute("PRAGMA quick_check").fetchone()
                if q[0] != "ok":
                    r["errors"].append(f"Check failed: {q[0]}")
                    full = conn.execute("PRAGMA integrity_check").fetchall()
                    r["errors"].extend(row[0] for row in full if row[0] != "ok")
                    return r
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk:
                    r["errors"].extend(f"FK violation: {row[0]}/{row[1]}" for row in fk)
                r["healthy"] = len(r["errors"]) == 0
        except sqlite3.Error as e:
            r["errors"].append(str(e))
        return r

    def _quarantine_db(self):
        try:
            d = Path(BACKUP_DIR) / "emergency"
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S%f")
            dest = d / ("unverified_%s.db" % ts)
            shutil.copy2(self.db_path, str(dest))
            logger.warning("Quarantined unverified database at %s", dest)
        except Exception as e:
            logger.error("Quarantine failed: %s", e, exc_info=True)

    def _handle_corruption(self):
        logger.error("Database integrity check failed; attempting automatic recovery")
        bp = self._create_emergency_backup("corruption")
        try:
            with self.connection_pool.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        except Exception as e:
            logger.warning("VACUUM after corruption failed: %s", e)
        if self._restore_backup():
            return
        if bp and self._restore_specific(bp):
            return
        self._init_new()

    def _create_emergency_backup(self, reason: str):
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S%f")
            d = Path(BACKUP_DIR) / "emergency"
            d.mkdir(parents=True, exist_ok=True)
            bp = d / ("emergency_%s_%s.db" % (reason, ts))
            bp_str = self._validate_backup_path(str(bp))
            with self.connection_pool.get_connection() as conn:
                conn.execute("VACUUM INTO ?", (bp_str,))
            with sqlite3.connect(bp_str) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    Path(bp_str).unlink(missing_ok=True)
                    return None
            return bp_str
        except Exception as e:
            logger.warning("Emergency backup failed: %s", e)
            return None

    def _migrate(self):
        bp = None
        try:
            with self.connection_pool.get_connection() as conn:
                cv = conn.execute("PRAGMA user_version").fetchone()[0]
                if cv > MAX_SCHEMA_VERSION:
                    logger.warning("Database schema v%d is newer than this build (v%d)", cv, MAX_SCHEMA_VERSION)
                needed = [(v, sql) for v, sql in sorted(self._migrations().items()) if v > cv]
            if not needed:
                return
            try:
                bp = self._create_emergency_backup("pre_migration")
            except Exception as e:
                logger.warning("Pre-migration backup skipped: %s", e)
                bp = None
            with self.connection_pool.get_connection() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                try:
                    for v, sql in needed:
                        self._exec_migration(conn, sql)
                        conn.commit()
                        conn.execute("PRAGMA user_version=%d" % v)
                        conn.commit()
                finally:
                    conn.execute("PRAGMA foreign_keys=ON")
            logger.info("Database migrated through schema version %d", needed[-1][0])
        except Exception as e:
            logger.error("Migration failed: %s", e, exc_info=True)
            if bp:
                if self._restore_specific(bp):
                    logger.warning("Restored pre-migration backup after failure")
            raise

    @staticmethod
    def _exec_migration(conn, sql: str):
        for stmt in (s.strip() for s in sql.split(';')):
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                # Makes re-running a partially applied migration safe.
                if "duplicate column name" in str(e).lower():
                    logger.warning("Migration statement skipped (column exists): %s", e)
                    continue
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
CREATE INDEX IF NOT EXISTS idx_mt_vessel ON maintenance_tasks(vessel_id);
CREATE INDEX IF NOT EXISTS idx_mt_status ON maintenance_tasks(status);
CREATE INDEX IF NOT EXISTS idx_ii_supplier ON inventory_items(supplier_id);
CREATE INDEX IF NOT EXISTS idx_tl_vessel ON trip_logs(vessel_id);
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
    currency TEXT DEFAULT 'TTD',
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
    ('currency', 'TTD'),
    ('fiscal_year_start', '01-01'),
    ('gst_rate', '12.5'),
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
UPDATE app_settings SET value='8' WHERE key='schema_version';
""",
            9: """
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('prune_old_data', 'false');
UPDATE app_settings SET value='9' WHERE key='schema_version';
""",
            10: """
ALTER TABLE vessels ADD COLUMN flag_state TEXT;
ALTER TABLE vessels ADD COLUMN port_of_registry TEXT;
ALTER TABLE vessels ADD COLUMN official_number TEXT;
ALTER TABLE vessels ADD COLUMN imo_number TEXT;
ALTER TABLE vessels ADD COLUMN registration_expiry TEXT;
CREATE INDEX IF NOT EXISTS idx_vessels_imo ON vessels(imo_number);
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('jurisdiction', 'tt');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('country', 'TT');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('locale', 'en_TT');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('timezone', 'America/Port_of_Spain');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('date_format', 'DD/MM/YYYY');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('tax_regime', 'VAT');
INSERT OR IGNORE INTO app_settings(key, value) VALUES ('tax_rate', '12.5');
UPDATE app_settings SET value='10' WHERE key='schema_version';
""",
        }

    def _init_new(self):
        self.connection_pool.close_all()
        self._remove_sidecars()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.connection_pool = DatabaseConnectionPool(self.db_path, max_connections=3)
        with self.connection_pool.get_connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                for v, sql in sorted(self._migrations().items()):
                    self._exec_migration(conn, sql)
                    conn.commit()
                    conn.execute("PRAGMA user_version=%d" % v)
                    conn.commit()
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
        with self._vlock:
            self._vcache.clear()
        logger.info("Initialized new database at %s", self.db_path)

    def _backup_candidates(self):
        root = Path(BACKUP_DIR)
        normal = [p for p in root.glob("*.db") if p.is_file()]
        emergency_dir = root / "emergency"
        emergency = [p for p in emergency_dir.glob("*.db") if p.is_file()] if emergency_dir.exists() else []
        key = lambda p: p.stat().st_mtime
        return sorted(normal + emergency, key=key, reverse=True)

    def _restore_backup(self):
        try:
            for candidate in self._backup_candidates():
                if candidate.name.startswith("unverified_"):
                    continue
                if self._restore_specific(str(candidate)):
                    logger.warning("Restored database from backup %s", candidate)
                    return True
        except Exception as e:
            logger.error("Backup restore failed: %s", e, exc_info=True)
        return False

    def _restore_specific(self, p: str) -> bool:
        try:
            with sqlite3.connect(p) as t:
                if t.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    logger.warning("Refusing to restore unhealthy backup %s", p)
                    return False
        except sqlite3.Error as e:
            logger.warning("Backup %s is unreadable: %s", p, e)
            return False
        try:
            self.connection_pool.close_all()
            self._remove_sidecars()
            shutil.copy2(p, self.db_path)
            self.connection_pool = DatabaseConnectionPool(self.db_path, max_connections=3)
            with self._vlock:
                self._vcache.clear()
            return True
        except Exception as e:
            logger.error("Restore from %s failed: %s", p, e, exc_info=True)
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
                raise DatabaseError("DB error: %s" % e) from e
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
        r = self.q("SELECT * FROM vessels WHERE id=?", (vid,))
        return r[0] if r else None

    def add_vessel(self, v):
        DataValidator.validate_vessel(v)
        hours = DataValidator.to_float(v.engine_hours, "Engine hours")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rid = self.ins("""INSERT INTO vessels(name,vessel_type,engine_make,engine_model,current_location,
            engine_hours,last_hours_update,flag_state,port_of_registry,official_number,imo_number,
            registration_expiry) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (v.name.strip(), v.vessel_type.strip(), v.engine_make.strip(), v.engine_model.strip(),
                        v.current_location.strip(), hours, now, (v.flag_state or "").strip(),
                        (v.port_of_registry or "").strip(), (v.official_number or "").strip(),
                        (v.imo_number or "").strip(), (v.registration_expiry or "").strip()))
        with self._vlock:
            self._vcache[rid] = v.name.strip()
        self.audit("create", "vessel", rid, new_values=v.__dict__)
        return rid

    def update_vessel(self, v):
        DataValidator.validate_vessel(v)
        old = self.get_vessel(v.id)
        hours = DataValidator.to_float(v.engine_hours, "Engine hours")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stamp = now
        if old and abs((old.get('engine_hours') or 0.0) - hours) < 1e-9:
            stamp = old.get('last_hours_update') or now
        rc = self.u("""UPDATE vessels SET name=?,vessel_type=?,engine_make=?,engine_model=?,
            current_location=?,engine_hours=?,last_hours_update=?,flag_state=?,port_of_registry=?,
            official_number=?,imo_number=?,registration_expiry=? WHERE id=?""",
                    (v.name.strip(), v.vessel_type.strip(), v.engine_make.strip(), v.engine_model.strip(),
                     v.current_location.strip(), hours, stamp, (v.flag_state or "").strip(),
                     (v.port_of_registry or "").strip(), (v.official_number or "").strip(),
                     (v.imo_number or "").strip(), (v.registration_expiry or "").strip(), v.id))
        with self._vlock:
            self._vcache[v.id] = v.name.strip()
        if rc:
            self.audit("update", "vessel", v.id, old_values=old, new_values=v.__dict__)
        return rc

    def delete_vessel(self, vid):
        old = self.get_vessel(vid)
        rc = self.u("DELETE FROM vessels WHERE id=?", (vid,))
        with self._vlock:
            self._vcache.pop(vid, None)
        if rc:
            self.audit("delete", "vessel", vid, old_values=old)
        return rc

    def vessel_count(self):
        r = self.q("SELECT COUNT(*) c FROM vessels")
        return r[0]['c'] if r else 0

    # ---- Maintenance ----
    def get_tasks_with_vessels(self):
        with self._vlock:
            if not self._vcache:
                self.get_vessels()
        tasks = self.q("SELECT * FROM maintenance_tasks ORDER BY next_due")
        with self._vlock:
            for t in tasks:
                t['vessel_name'] = self._vcache.get(t['vessel_id'], 'Unknown')
        return tasks

    def get_tasks(self):
        return self.get_tasks_with_vessels()

    def get_task(self, tid):
        tasks = self.get_tasks_with_vessels()
        return next((t for t in tasks if t['id'] == tid), None)

    def add_task(self, t):
        DataValidator.validate_task(t)
        wo = (t.work_order or "").strip() or self.next_work_order()
        rid = self.ins("""INSERT INTO maintenance_tasks(vessel_id,system,task_name,description,next_due,status,priority,
            interval_days,interval_hours,is_recurring,work_order,cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (t.vessel_id, t.system.strip(), t.task_name.strip(), t.description.strip(), t.next_due.strip(),
                        t.status, t.priority, t.interval_days, t.interval_hours, int(t.is_recurring), wo, t.cost))
        self.audit("create", "task", rid, new_values=t.__dict__)
        return rid

    def update_task(self, t):
        if t.status == 'overdue' and t.next_due and t.next_due >= datetime.date.today().isoformat():
            t.status = 'pending'
        DataValidator.validate_task(t)
        old = self.get_task(t.id)
        rc = self.u("""UPDATE maintenance_tasks SET vessel_id=?,system=?,task_name=?,description=?,next_due=?,status=?,priority=?,
            interval_days=?,interval_hours=?,is_recurring=?,work_order=?,cost=? WHERE id=?""",
                    (t.vessel_id, t.system.strip(), t.task_name.strip(), t.description.strip(), t.next_due.strip(),
                     t.status, t.priority, t.interval_days, t.interval_hours, int(t.is_recurring), t.work_order, t.cost, t.id))
        if rc:
            self.audit("update", "task", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_task(self, tid):
        old = self.get_task(tid)
        rc = self.u("DELETE FROM maintenance_tasks WHERE id=?", (tid,))
        if rc:
            self.audit("delete", "task", tid, old_values=old)
        return rc

    def task_counts(self, due_soon_days=7):
        self.check_overdue_tasks()
        r = self.q("SELECT status,COUNT(*) c FROM maintenance_tasks GROUP BY status")
        counts = {row['status']: row['c'] for row in r}
        due = self.q("SELECT COUNT(*) c FROM maintenance_tasks WHERE status='pending' AND next_due IS NOT NULL AND next_due<=date('now', ?)",
                     ('+%d days' % int(due_soon_days),))
        return {'total': sum(counts.values()), 'pending': counts.get('pending', 0),
                'in_progress': counts.get('in_progress', 0), 'completed': counts.get('completed', 0),
                'overdue': counts.get('overdue', 0), 'due_soon': due[0]['c'] if due else 0}

    # ---- Inventory ----
    def get_items(self):
        return self.q("SELECT * FROM inventory_items ORDER BY name")

    def get_item(self, iid):
        r = self.q("SELECT * FROM inventory_items WHERE id=?", (iid,))
        return r[0] if r else None

    def add_item(self, name, pn, cat, stock, minstock, cost, supplier_id=None):
        name = DataValidator.validate_item_name(name)
        DataValidator.validate_stock(stock, minstock)
        cost = DataValidator.to_float(cost, "Unit cost")
        if cost < 0:
            raise ValidationError("Unit cost cannot be negative")
        with self.cursor() as c:
            c.execute("INSERT INTO inventory_items(name,part_number,category,current_stock,minimum_stock,unit_cost,supplier_id) VALUES(?,?,?,?,?,?,?)",
                      (name, pn.strip(), cat.strip(), stock, minstock, cost, supplier_id or None))
            rid = c.lastrowid
            if stock:
                c.execute("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,'add','Initial stock')",
                          (rid, stock))
        self.audit("create", "inventory_item", rid,
                   new_values={"name": name, "part_number": pn, "category": cat, "stock": stock,
                               "minstock": minstock, "cost": cost, "supplier_id": supplier_id})
        return rid

    def update_item(self, iid, name, pn, cat, stock, minstock, cost, supplier_id=None):
        old = self.get_item(iid)
        name = DataValidator.validate_item_name(name)
        DataValidator.validate_stock(stock, minstock)
        cost = DataValidator.to_float(cost, "Unit cost")
        if cost < 0:
            raise ValidationError("Unit cost cannot be negative")
        with self.cursor() as c:
            c.execute("UPDATE inventory_items SET name=?,part_number=?,category=?,current_stock=?,minimum_stock=?,unit_cost=?,supplier_id=? WHERE id=?",
                      (name, pn.strip(), cat.strip(), stock, minstock, cost, supplier_id or None, iid))
            rc = c.rowcount
            if rc and old is not None and (old.get('current_stock') or 0) != stock:
                delta = stock - (old.get('current_stock') or 0)
                c.execute("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,?,?)",
                          (iid, abs(delta), "add" if delta > 0 else "remove", "Manual stock edit"))
        if rc:
            self.audit("update", "inventory_item", iid, old_values=old,
                       new_values={"name": name, "part_number": pn, "category": cat, "stock": stock,
                                   "minstock": minstock, "cost": cost, "supplier_id": supplier_id})
        return rc

    def delete_item(self, iid):
        old = self.get_item(iid)
        rc = self.u("DELETE FROM inventory_items WHERE id=?", (iid,))
        if rc:
            self.audit("delete", "inventory_item", iid, old_values=old)
        return rc

    def adjust_stock(self, iid, delta, notes=""):
        with self.cursor() as c:
            row = c.execute("SELECT * FROM inventory_items WHERE id=?", (iid,)).fetchone()
            if not row:
                raise ValidationError("Item not found")
            item = dict(row)
            new_stock = (item.get('current_stock') or 0) + delta
            if new_stock < 0:
                raise ValidationError("Insufficient stock (have %d, need %d)" % (item.get('current_stock') or 0, -delta))
            c.execute("UPDATE inventory_items SET current_stock=? WHERE id=?", (new_stock, iid))
            tx_type = "add" if delta > 0 else "remove"
            c.execute("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,?,?)",
                      (iid, abs(delta), tx_type, (notes or "").strip()))
        self.audit("stock_adjust", "inventory_item", iid,
                   new_values={"current_stock": new_stock, "delta": delta, "notes": notes})
        return new_stock

    def inventory_summary(self):
        items = self.get_items()
        low = [i for i in items if i['current_stock'] <= i['minimum_stock']]
        return {'total': len(items), 'low_count': len(low), 'low_items': low,
                'total_value': sum(i['current_stock'] * (i['unit_cost'] or 0) for i in items)}

    # ---- Trip Logs ----
    def get_trips(self):
        trips = self.q("SELECT * FROM trip_logs ORDER BY departure DESC")
        with self._vlock:
            if not self._vcache:
                self.get_vessels()
            for t in trips:
                t['vessel_name'] = self._vcache.get(t['vessel_id'], 'Unknown')
        return trips

    def add_trip(self, t):
        if t.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        t.distance = DataValidator.to_float(t.distance, "Distance")
        t.fuel_used = DataValidator.to_float(t.fuel_used, "Fuel used")
        if t.distance < 0 or t.fuel_used < 0:
            raise ValidationError("Distance and fuel used cannot be negative")
        rid = self.ins("INSERT INTO trip_logs(vessel_id,departure,arrival,destination,distance,fuel_used) VALUES(?,?,?,?,?,?)",
                       (t.vessel_id, t.departure.strip(), t.arrival.strip(), t.destination.strip(), t.distance, t.fuel_used))
        self.audit("create", "trip", rid, new_values=t.__dict__)
        return rid

    def update_trip(self, t):
        if t.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        t.distance = DataValidator.to_float(t.distance, "Distance")
        t.fuel_used = DataValidator.to_float(t.fuel_used, "Fuel used")
        if t.distance < 0 or t.fuel_used < 0:
            raise ValidationError("Distance and fuel used cannot be negative")
        old = self.q("SELECT * FROM trip_logs WHERE id=?", (t.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE trip_logs SET vessel_id=?,departure=?,arrival=?,destination=?,distance=?,fuel_used=? WHERE id=?",
                    (t.vessel_id, t.departure.strip(), t.arrival.strip(), t.destination.strip(), t.distance, t.fuel_used, t.id))
        if rc:
            self.audit("update", "trip", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_trip(self, tid):
        old = self.q("SELECT * FROM trip_logs WHERE id=?", (tid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM trip_logs WHERE id=?", (tid,))
        if rc:
            self.audit("delete", "trip", tid, old_values=old)
        return rc

    # ---- Suppliers ----
    def get_suppliers(self):
        return self.q("SELECT * FROM suppliers ORDER BY name")

    def add_supplier(self, s):
        DataValidator.validate_supplier(s)
        rid = self.ins("INSERT INTO suppliers(name,contact,phone,email,preferred) VALUES(?,?,?,?,?)",
                       (s.name.strip(), s.contact.strip(), s.phone.strip(), s.email.strip(), 1 if s.preferred else 0))
        self.audit("create", "supplier", rid, new_values=s.__dict__)
        return rid

    def update_supplier(self, s):
        DataValidator.validate_supplier(s)
        old = self.q("SELECT * FROM suppliers WHERE id=?", (s.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE suppliers SET name=?,contact=?,phone=?,email=?,preferred=? WHERE id=?",
                    (s.name.strip(), s.contact.strip(), s.phone.strip(), s.email.strip(), 1 if s.preferred else 0, s.id))
        if rc:
            self.audit("update", "supplier", s.id, old_values=old, new_values=s.__dict__)
        return rc

    def delete_supplier(self, sid):
        old = self.q("SELECT * FROM suppliers WHERE id=?", (sid,))
        old = old[0] if old else None
        with self.cursor() as c:
            c.execute("UPDATE inventory_items SET supplier_id=NULL WHERE supplier_id=?", (sid,))
            c.execute("DELETE FROM suppliers WHERE id=?", (sid,))
            rc = c.rowcount
        if rc:
            self.audit("delete", "supplier", sid, old_values=old)
        return rc

    # ---- Stock Transactions ----
    def get_transactions(self, iid=None):
        if iid:
            return self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id WHERE st.item_id=? ORDER BY st.created_at DESC", (iid,))
        return self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id ORDER BY st.created_at DESC")

    def delete_stock_transaction(self, tid):
        with self.cursor() as c:
            row = c.execute("SELECT * FROM stock_transactions WHERE id=?", (tid,)).fetchone()
            if not row:
                return 0
            tx = dict(row)
            item = c.execute("SELECT current_stock FROM inventory_items WHERE id=?", (tx['item_id'],)).fetchone()
            if item is not None:
                qty = tx.get('quantity') or 0
                delta = -qty if tx.get('transaction_type') == 'add' else qty
                new_stock = max(0, (item['current_stock'] or 0) + delta)
                c.execute("UPDATE inventory_items SET current_stock=? WHERE id=?", (new_stock, tx['item_id']))
        self.audit("delete", "stock_transaction", tid, old_values=tx)
        return 1

    # ---- Backup ----
    def create_backup(self, path: str) -> bool:
        vpath = None
        try:
            vpath = self._validate_backup_path(path)
            with self.connection_pool.get_connection() as conn:
                conn.execute("VACUUM INTO ?", (vpath,))
            with sqlite3.connect(vpath) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0] == "ok":
                    return True
            os.remove(vpath)
            return False
        except (sqlite3.Error, OSError, ValidationError) as e:
            logger.warning("Backup to %s failed: %s", path, e)
            if vpath and os.path.exists(vpath):
                try:
                    os.remove(vpath)
                except OSError:
                    pass
            return False

    # ---- Audit Log ----
    def audit(self, action: str, entity_type: str = None, entity_id: int = None,
              old_values: dict = None, new_values: dict = None, session_id: str = None):
        try:
            self.ins("INSERT INTO audit_log(action,entity_type,entity_id,old_values,new_values,session_id) VALUES(?,?,?,?,?,?)",
                     (action, entity_type, entity_id,
                      json.dumps(old_values, default=str) if old_values else None,
                      json.dumps(new_values, default=str) if new_values else None,
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

    def _validate_expense(self, e: Expense):
        if not e.category:
            raise ValidationError("Expense category is required")
        e.amount = DataValidator.to_float(e.amount, "Amount")
        if e.amount <= 0:
            raise ValidationError("Amount must be positive")
        if not e.expense_date:
            raise ValidationError("Expense date is required")
        e.expense_date = DataValidator.date_optional(e.expense_date, "Expense date")
        e.gst_rate = DataValidator.to_float(e.gst_rate, "Tax rate")
        if e.gst_rate < 0 or e.gst_rate > 100:
            raise ValidationError("Tax rate must be between 0 and 100")
        e.currency = (e.currency or DEFAULT_CURRENCY).strip().upper() or DEFAULT_CURRENCY
        e.gst_amount = round(e.amount * e.gst_rate / 100.0, 2) if e.gst_rate else 0.0
        if len(e.currency) != 3 or not e.currency.isalpha():
            raise ValidationError("Currency must be a three-letter ISO code (for example TTD)")
        if e.trip_id is not None and (not isinstance(e.trip_id, int) or e.trip_id <= 0):
            e.trip_id = None

    def add_expense(self, e: Expense) -> int:
        self._validate_expense(e)
        eid = self.ins("""INSERT INTO expenses(vessel_id,trip_id,category,amount,currency,expense_date,description,vendor,receipt_path,tax_deductible,tax_category,gst_rate,gst_amount)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (e.vessel_id if e.vessel_id > 0 else None,
                         e.trip_id, e.category, e.amount, e.currency,
                         e.expense_date, e.description, e.vendor,
                         e.receipt_path, 1 if e.tax_deductible else 0,
                         e.tax_category, e.gst_rate, e.gst_amount))
        self.audit("create", "expense", eid,
                   new_values={"amount": e.amount, "category": e.category, "gst_amount": e.gst_amount})
        return eid

    def update_expense(self, e: Expense) -> int:
        self._validate_expense(e)
        old = self.get_expense(e.id)
        rc = self.u("""UPDATE expenses SET vessel_id=?,trip_id=?,category=?,amount=?,currency=?,expense_date=?,description=?,vendor=?,receipt_path=?,tax_deductible=?,tax_category=?,gst_rate=?,gst_amount=? WHERE id=?""",
                    (e.vessel_id if e.vessel_id > 0 else None, e.trip_id, e.category, e.amount,
                     e.currency, e.expense_date, e.description, e.vendor, e.receipt_path,
                     1 if e.tax_deductible else 0, e.tax_category, e.gst_rate, e.gst_amount, e.id))
        if rc:
            self.audit("update", "expense", e.id, old_values=old,
                       new_values={"amount": e.amount, "category": e.category, "gst_amount": e.gst_amount})
        return rc

    def delete_expense(self, eid: int) -> int:
        old = self.get_expense(eid)
        rc = self.u("DELETE FROM expenses WHERE id=?", (eid,))
        if rc:
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

    def _validate_budget(self, b: Budget):
        if b.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        if not b.category:
            raise ValidationError("Budget category is required")
        b.budget_amount = DataValidator.to_float(b.budget_amount, "Budget amount")
        if b.budget_amount < 0:
            raise ValidationError("Budget amount cannot be negative")
        if not b.fiscal_year:
            b.fiscal_year = datetime.datetime.now().year
        try:
            b.fiscal_year = int(b.fiscal_year)
        except (TypeError, ValueError):
            raise ValidationError("Fiscal year must be a whole number")
        if not (1900 <= b.fiscal_year <= 2200):
            raise ValidationError("Fiscal year must be between 1900 and 2200")

    def add_budget(self, b: Budget) -> int:
        self._validate_budget(b)
        bid = self.ins("INSERT INTO budgets(vessel_id,fiscal_year,category,budget_amount,notes) VALUES(?,?,?,?,?)",
                       (b.vessel_id, b.fiscal_year, b.category, b.budget_amount, b.notes))
        self.audit("create", "budget", bid, new_values={"amount": b.budget_amount, "category": b.category,
                                                        "fiscal_year": b.fiscal_year})
        return bid

    def update_budget(self, b: Budget) -> int:
        self._validate_budget(b)
        old = self.q("SELECT * FROM budgets WHERE id=?", (b.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE budgets SET vessel_id=?,fiscal_year=?,category=?,budget_amount=?,notes=? WHERE id=?",
                    (b.vessel_id, b.fiscal_year, b.category, b.budget_amount, b.notes, b.id))
        if rc:
            self.audit("update", "budget", b.id, old_values=old,
                       new_values={"amount": b.budget_amount, "category": b.category})
        return rc

    def delete_budget(self, bid: int) -> int:
        old = self.q("SELECT * FROM budgets WHERE id=?", (bid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM budgets WHERE id=?", (bid,))
        if rc:
            self.audit("delete", "budget", bid, old_values=old)
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
        if fl.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        fl.gallons = DataValidator.to_float(fl.gallons, "Gallons")
        if fl.gallons <= 0:
            raise ValidationError("Gallons must be positive")
        fl.cost_per_gallon = DataValidator.to_float(fl.cost_per_gallon, "Cost per gallon")
        fl.total_cost = DataValidator.to_float(fl.total_cost, "Total cost")
        if fl.cost_per_gallon < 0 or fl.total_cost < 0:
            raise ValidationError("Fuel costs cannot be negative")
        fl.engine_hours = DataValidator.to_float(fl.engine_hours, "Engine hours")
        if fl.engine_hours < 0:
            raise ValidationError("Engine hours cannot be negative")
        total = fl.total_cost or (fl.gallons * (fl.cost_per_gallon or 0.0))
        fid = self.ins("""INSERT INTO fuel_logs(trip_id,vessel_id,gallons,cost_per_gallon,total_cost,fuel_type,vendor,location,engine_hours)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (fl.trip_id, fl.vessel_id, fl.gallons, fl.cost_per_gallon, total,
                        fl.fuel_type, fl.vendor, fl.location, fl.engine_hours))
        self.audit("create", "fuel_log", fid, new_values={"gallons": fl.gallons, "total_cost": total})
        return fid

    def update_fuel_log(self, fl: FuelLog) -> int:
        if fl.vessel_id <= 0:
            raise ValidationError("Select a vessel")
        fl.gallons = DataValidator.to_float(fl.gallons, "Gallons")
        if fl.gallons <= 0:
            raise ValidationError("Gallons must be positive")
        fl.cost_per_gallon = DataValidator.to_float(fl.cost_per_gallon, "Cost per gallon")
        fl.total_cost = DataValidator.to_float(fl.total_cost, "Total cost")
        if fl.cost_per_gallon < 0 or fl.total_cost < 0:
            raise ValidationError("Fuel costs cannot be negative")
        fl.engine_hours = DataValidator.to_float(fl.engine_hours, "Engine hours")
        if fl.engine_hours < 0:
            raise ValidationError("Engine hours cannot be negative")
        total = fl.total_cost or (fl.gallons * (fl.cost_per_gallon or 0.0))
        old = self.q("SELECT * FROM fuel_logs WHERE id=?", (fl.id,))
        old = old[0] if old else None
        rc = self.u("""UPDATE fuel_logs SET trip_id=?,vessel_id=?,gallons=?,cost_per_gallon=?,total_cost=?,fuel_type=?,vendor=?,location=?,engine_hours=? WHERE id=?""",
                    (fl.trip_id, fl.vessel_id, fl.gallons, fl.cost_per_gallon, total,
                     fl.fuel_type, fl.vendor, fl.location, fl.engine_hours, fl.id))
        if rc:
            self.audit("update", "fuel_log", fl.id, old_values=old,
                       new_values={"gallons": fl.gallons, "total_cost": total})
        return rc

    def delete_fuel_log(self, fid: int) -> int:
        old = self.q("SELECT * FROM fuel_logs WHERE id=?", (fid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM fuel_logs WHERE id=?", (fid,))
        if rc:
            self.audit("delete", "fuel_log", fid, old_values=old)
        return rc

    def fuel_efficiency(self, vessel_id: int = None):
        sql = """SELECT fl.vessel_id,v.name vessel_name,
                 SUM(fl.gallons) total_gallons,SUM(fl.total_cost) total_fuel_cost
                 FROM fuel_logs fl LEFT JOIN vessels v ON fl.vessel_id=v.id"""
        params = []
        if vessel_id:
            sql += " WHERE fl.vessel_id=?"
            params.append(vessel_id)
        sql += " GROUP BY fl.vessel_id"
        rows = self.q(sql, tuple(params))
        # Distance is counted once per trip even when several fuel logs reference it.
        dsql = """SELECT t.vessel_id,SUM(tl.distance) total_distance
                  FROM (SELECT DISTINCT fl2.vessel_id,fl2.trip_id FROM fuel_logs fl2 WHERE fl2.trip_id IS NOT NULL) t
                  JOIN trip_logs tl ON tl.id=t.trip_id"""
        dparams = []
        if vessel_id:
            dsql += " WHERE t.vessel_id=?"
            dparams.append(vessel_id)
        dsql += " GROUP BY t.vessel_id"
        distances = {r['vessel_id']: (r['total_distance'] or 0) for r in self.q(dsql, tuple(dparams))}
        for r in rows:
            dist = distances.get(r['vessel_id'], 0)
            gallons = r.get('total_gallons') or 0
            r['total_distance'] = dist
            r['nmpg'] = (dist / gallons) if gallons else 0
        rows.sort(key=lambda r: r.get('nmpg', 0), reverse=True)
        return rows

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
    def get_setting(self, key: str, default: str = "") -> str:
        r = self.q("SELECT value FROM app_settings WHERE key=?", (key,))
        return r[0]['value'] if r else default

    def set_setting(self, key: str, value: str):
        self.u("INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,datetime('now'))", (key, value))

    @staticmethod
    def _retention_days(value, default):
        try:
            d = int(str(value).strip())
            return d if d > 0 else default
        except (TypeError, ValueError):
            return default

    def prune_old_data(self):
        enabled = str(self.get_setting("prune_old_data", "false")).strip().lower()
        if enabled not in ("1", "true", "yes", "on"):
            return
        audit_days = self._retention_days(self.get_setting("audit_retention_days", "365"), 365)
        data_days = self._retention_days(self.get_setting("data_retention_days", "730"), 730)
        try:
            removed = self.u("DELETE FROM audit_log WHERE timestamp < datetime('now', ?)", ("-%d days" % audit_days,))
            if removed:
                logger.info("Pruned %d audit entries older than %d days", removed, audit_days)
        except Exception as e:
            logger.warning("Audit prune failed: %s", e)
        for table in ("expenses", "trip_logs", "fuel_logs"):
            try:
                removed = self.u("DELETE FROM %s WHERE created_at < datetime('now', ?)" % table, ("-%d days" % data_days,))
                if removed:
                    logger.warning("Pruned %d rows from %s older than %d days", removed, table, data_days)
            except Exception as e:
                logger.warning("Prune of %s failed: %s", table, e)

    # ---- GDPR Data Export / Erasure ----
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
            "stock_transactions": self.q("SELECT * FROM stock_transactions"),
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
        # Delete everything in one transaction so erasure cannot leave a
        # half-populated database behind if a statement fails.
        with self.cursor() as c:
            for table in ['watch_schedules', 'training_records', 'certifications', 'crew_members',
                          'fuel_logs', 'expenses', 'budgets', 'stock_transactions',
                          'task_parts', 'task_templates', 'maintenance_tasks', 'trip_logs',
                          'inventory_items', 'suppliers', 'vessels']:
                c.execute("DELETE FROM %s" % table)
            # Audit snapshots can contain personal data, so they are erased too.
            c.execute("DELETE FROM audit_log")
        with self._vlock:
            self._vcache.clear()
        self.audit("gdpr_delete", "all", None)

    # ---- Crew ----
    def get_crew_members(self):
        return self.q("SELECT * FROM crew_members ORDER BY last_name,first_name")

    def get_crew_member(self, cid):
        r = self.q("SELECT * FROM crew_members WHERE id=?", (cid,))
        return r[0] if r else None

    def add_crew_member(self, c):
        DataValidator.validate_crew_member(c)
        rid = self.ins("INSERT INTO crew_members(first_name,last_name,email,phone,emergency_contact,emergency_phone,role,date_joined,date_left,status,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (c.first_name.strip(), c.last_name.strip(), c.email.strip(), c.phone.strip(),
                        c.emergency_contact.strip(), c.emergency_phone.strip(), c.role.strip(),
                        c.date_joined.strip(), c.date_left.strip(), c.status, c.notes.strip()))
        self.audit("create", "crew_member", rid, new_values=c.__dict__)
        return rid

    def update_crew_member(self, c):
        DataValidator.validate_crew_member(c)
        old = self.get_crew_member(c.id)
        rc = self.u("UPDATE crew_members SET first_name=?,last_name=?,email=?,phone=?,emergency_contact=?,emergency_phone=?,role=?,date_joined=?,date_left=?,status=?,notes=? WHERE id=?",
                    (c.first_name.strip(), c.last_name.strip(), c.email.strip(), c.phone.strip(),
                     c.emergency_contact.strip(), c.emergency_phone.strip(), c.role.strip(),
                     c.date_joined.strip(), c.date_left.strip(), c.status, c.notes.strip(), c.id))
        if rc:
            self.audit("update", "crew_member", c.id, old_values=old, new_values=c.__dict__)
        return rc

    def delete_crew_member(self, cid):
        old = self.get_crew_member(cid)
        rc = self.u("DELETE FROM crew_members WHERE id=?", (cid,))
        if rc:
            self.audit("delete", "crew_member", cid, old_values=old)
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
        rid = self.ins("INSERT INTO certifications(crew_member_id,name,issuing_body,cert_number,category,issue_date,expiry_date) VALUES(?,?,?,?,?,?,?)",
                       (c.crew_member_id, c.name.strip(), c.issuing_body.strip(), c.cert_number.strip(),
                        c.category.strip(), c.issue_date.strip(), c.expiry_date.strip()))
        self.audit("create", "certification", rid, new_values=c.__dict__)
        return rid

    def update_certification(self, c):
        DataValidator.validate_certification(c)
        old = self.q("SELECT * FROM certifications WHERE id=?", (c.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE certifications SET crew_member_id=?,name=?,issuing_body=?,cert_number=?,category=?,issue_date=?,expiry_date=? WHERE id=?",
                    (c.crew_member_id, c.name.strip(), c.issuing_body.strip(), c.cert_number.strip(),
                     c.category.strip(), c.issue_date.strip(), c.expiry_date.strip(), c.id))
        if rc:
            self.audit("update", "certification", c.id, old_values=old, new_values=c.__dict__)
        return rc

    def delete_certification(self, cid):
        old = self.q("SELECT * FROM certifications WHERE id=?", (cid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM certifications WHERE id=?", (cid,))
        if rc:
            self.audit("delete", "certification", cid, old_values=old)
        return rc

    # ---- Training Records ----
    def get_training_records(self, crew_id=None):
        if crew_id:
            return self.q("SELECT t.*,cm.first_name||' '||cm.last_name crew_member FROM training_records t LEFT JOIN crew_members cm ON t.crew_member_id=cm.id WHERE t.crew_member_id=? ORDER BY t.date_completed DESC", (crew_id,))
        return self.q("SELECT t.*,cm.first_name||' '||cm.last_name crew_member FROM training_records t LEFT JOIN crew_members cm ON t.crew_member_id=cm.id ORDER BY t.date_completed DESC")

    def add_training_record(self, t):
        DataValidator.validate_training(t)
        rid = self.ins("INSERT INTO training_records(crew_member_id,course_name,provider,date_completed,expiry_date,cost,notes) VALUES(?,?,?,?,?,?,?)",
                       (t.crew_member_id, t.course_name.strip(), t.provider.strip(), t.date_completed.strip(),
                        t.expiry_date.strip(), t.cost, t.notes.strip()))
        self.audit("create", "training_record", rid, new_values=t.__dict__)
        return rid

    def update_training_record(self, t):
        DataValidator.validate_training(t)
        old = self.q("SELECT * FROM training_records WHERE id=?", (t.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE training_records SET crew_member_id=?,course_name=?,provider=?,date_completed=?,expiry_date=?,cost=?,notes=? WHERE id=?",
                    (t.crew_member_id, t.course_name.strip(), t.provider.strip(), t.date_completed.strip(),
                     t.expiry_date.strip(), t.cost, t.notes.strip(), t.id))
        if rc:
            self.audit("update", "training_record", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_training_record(self, tid):
        old = self.q("SELECT * FROM training_records WHERE id=?", (tid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM training_records WHERE id=?", (tid,))
        if rc:
            self.audit("delete", "training_record", tid, old_values=old)
        return rc

    # ---- Watch Schedules ----
    def get_watch_schedules(self, vessel_id=None, watch_date=None):
        q = "SELECT ws.*,cm.first_name||' '||cm.last_name crew_name,v.name vessel_name FROM watch_schedules ws LEFT JOIN crew_members cm ON ws.crew_member_id=cm.id LEFT JOIN vessels v ON ws.vessel_id=v.id"
        params = []
        where = []
        if vessel_id:
            where.append("ws.vessel_id=?")
            params.append(vessel_id)
        if watch_date:
            where.append("ws.date=?")
            params.append(watch_date)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY ws.date,ws.start_time"
        return self.q(q, tuple(params))

    def add_watch_schedule(self, w):
        DataValidator.validate_watch_schedule(w)
        rid = self.ins("INSERT INTO watch_schedules(crew_member_id,vessel_id,date,start_time,end_time,role_on_watch,notes) VALUES(?,?,?,?,?,?,?)",
                       (w.crew_member_id, w.vessel_id if w.vessel_id > 0 else None,
                        w.date.strip(), w.start_time.strip(), w.end_time.strip(),
                        w.role_on_watch.strip(), w.notes.strip()))
        self.audit("create", "watch_schedule", rid, new_values=w.__dict__)
        return rid

    def update_watch_schedule(self, w):
        DataValidator.validate_watch_schedule(w)
        old = self.q("SELECT * FROM watch_schedules WHERE id=?", (w.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE watch_schedules SET crew_member_id=?,vessel_id=?,date=?,start_time=?,end_time=?,role_on_watch=?,notes=? WHERE id=?",
                    (w.crew_member_id, w.vessel_id if w.vessel_id > 0 else None,
                     w.date.strip(), w.start_time.strip(), w.end_time.strip(),
                     w.role_on_watch.strip(), w.notes.strip(), w.id))
        if rc:
            self.audit("update", "watch_schedule", w.id, old_values=old, new_values=w.__dict__)
        return rc

    def delete_watch_schedule(self, wid):
        old = self.q("SELECT * FROM watch_schedules WHERE id=?", (wid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM watch_schedules WHERE id=?", (wid,))
        if rc:
            self.audit("delete", "watch_schedule", wid, old_values=old)
        return rc

    # ---- Task Parts ----
    def get_task_parts(self, task_id):
        return self.q("SELECT tp.*,ii.name part_name FROM task_parts tp LEFT JOIN inventory_items ii ON tp.item_id=ii.id WHERE tp.task_id=? ORDER BY tp.id", (task_id,))

    def add_task_part(self, tp):
        DataValidator.validate_task_part(tp)
        rid = self.ins("INSERT INTO task_parts(task_id,item_id,quantity_used,cost_per_unit) VALUES(?,?,?,?)",
                       (tp.task_id, tp.item_id, tp.quantity_used, tp.cost_per_unit))
        self.audit("create", "task_part", rid, new_values=tp.__dict__)
        return rid

    def delete_task_part(self, tpid):
        old = self.q("SELECT * FROM task_parts WHERE id=?", (tpid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM task_parts WHERE id=?", (tpid,))
        if rc:
            self.audit("delete", "task_part", tpid, old_values=old)
        return rc

    # ---- Task Templates ----
    def get_task_templates(self):
        return self.q("SELECT * FROM task_templates ORDER BY name")

    def add_task_template(self, t):
        DataValidator.validate_template(t)
        rid = self.ins("INSERT INTO task_templates(name,system,description,priority,interval_days,interval_hours,default_vessel_type) VALUES(?,?,?,?,?,?,?)",
                       (t.name.strip(), t.system.strip(), t.description.strip(), t.priority, t.interval_days,
                        t.interval_hours, t.default_vessel_type.strip()))
        self.audit("create", "task_template", rid, new_values=t.__dict__)
        return rid

    def update_task_template(self, t):
        DataValidator.validate_template(t)
        old = self.q("SELECT * FROM task_templates WHERE id=?", (t.id,))
        old = old[0] if old else None
        rc = self.u("UPDATE task_templates SET name=?,system=?,description=?,priority=?,interval_days=?,interval_hours=?,default_vessel_type=? WHERE id=?",
                    (t.name.strip(), t.system.strip(), t.description.strip(), t.priority, t.interval_days,
                     t.interval_hours, t.default_vessel_type.strip(), t.id))
        if rc:
            self.audit("update", "task_template", t.id, old_values=old, new_values=t.__dict__)
        return rc

    def delete_task_template(self, tid):
        old = self.q("SELECT * FROM task_templates WHERE id=?", (tid,))
        old = old[0] if old else None
        rc = self.u("DELETE FROM task_templates WHERE id=?", (tid,))
        if rc:
            self.audit("delete", "task_template", tid, old_values=old)
        return rc

    # ---- Workflow ----
    def next_work_order(self):
        r = self.q("SELECT MAX(CAST(SUBSTR(work_order,4) AS INTEGER)) mx FROM maintenance_tasks WHERE work_order LIKE 'WO-%'")
        nxt = (r[0]['mx'] or 0) + 1 if r else 1
        return "WO-%05d" % nxt

    def complete_task(self, task_id, completed_by, completed_notes, cost):
        cost = DataValidator.to_float(cost, "Cost")
        if cost < 0:
            raise ValidationError("Cost cannot be negative")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.cursor() as c:
            task = c.execute("SELECT * FROM maintenance_tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ValidationError("Task not found")
            if task['status'] == 'completed':
                raise ValidationError("This task is already completed")
            c.execute("UPDATE maintenance_tasks SET status='completed',completed_at=?,completed_by=?,completed_notes=?,cost=? WHERE id=?",
                      (now, completed_by, completed_notes, cost, task_id))
            parts = c.execute("""SELECT tp.quantity_used,tp.item_id,ii.current_stock,ii.name item_name
                                 FROM task_parts tp JOIN inventory_items ii ON tp.item_id=ii.id
                                 WHERE tp.task_id=?""", (task_id,)).fetchall()
            for p in parts:
                have = p['current_stock'] or 0
                need = p['quantity_used'] or 0
                if need <= 0:
                    continue
                used = min(have, need)
                if used < need:
                    logger.warning("Part %s: needed %d, only %d in stock", p['item_name'], need, used)
                if used:
                    c.execute("UPDATE inventory_items SET current_stock=current_stock-? WHERE id=?", (used, p['item_id']))
                    c.execute("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,'remove',?)",
                              (p['item_id'], used, "Used on task %s" % (task['work_order'] or ("#%d" % task_id))))
            if task['is_recurring'] and task['next_due'] and task['interval_days']:
                try:
                    old_due = datetime.datetime.strptime(task['next_due'], "%Y-%m-%d")
                    step = datetime.timedelta(days=int(task['interval_days']))
                    new_due_dt = old_due + step
                    today = datetime.datetime.now()
                    # Never schedule a fresh recurring task in the past.
                    guard = 0
                    while new_due_dt.date() <= today.date() and guard < 10000:
                        new_due_dt += step
                        guard += 1
                    new_due = new_due_dt.strftime("%Y-%m-%d")
                    row = c.execute("SELECT MAX(CAST(SUBSTR(work_order,4) AS INTEGER)) mx FROM maintenance_tasks WHERE work_order LIKE 'WO-%'").fetchone()
                    wo = "WO-%05d" % ((row['mx'] or 0) + 1)
                    c.execute("""INSERT INTO maintenance_tasks(vessel_id,system,task_name,description,next_due,status,priority,interval_days,interval_hours,is_recurring,work_order)
                                 VALUES(?,?,?,?,?,'pending',?,?,?,1,?)""",
                              (task['vessel_id'], task['system'], task['task_name'], task['description'],
                               new_due, task['priority'], task['interval_days'], task['interval_hours'], wo))
                except (ValueError, sqlite3.Error) as e:
                    logger.warning("Recurring task creation failed for #%s: %s", task_id, e)
        self.audit("complete_task", "maintenance_task", task_id,
                   new_values={"status": "completed", "completed_by": completed_by,
                               "completed_notes": completed_notes, "cost": cost})

    def check_overdue_tasks(self):
        count = self.u("UPDATE maintenance_tasks SET status='overdue' WHERE status='pending' AND next_due IS NOT NULL AND next_due<date('now')")
        if count:
            logger.info("Auto-overdue: %d tasks marked overdue", count)
        return count

    # ---- Export ----
    def export_csv(self, path: str, query: str, params: tuple = (), comments: tuple = ()) -> int:
        if not str(path).lower().endswith(".csv"):
            path = str(path) + ".csv"
        p = Path(path)
        parent = p.parent
        if not parent.exists():
            raise ValidationError("Directory does not exist: %s" % parent)
        if not parent.is_dir():
            raise ValidationError("Not a directory: %s" % parent)
        rows = self.q(query, params)
        if not rows:
            return 0
        with open(path, 'w', newline='', encoding='utf-8') as f:
            # Comment lines are prefixed so spreadsheet tools still open the
            # file, while keeping the export's provenance and disclaimer.
            for line in comments or ():
                text = " ".join(str(line).splitlines()).strip()
                if text:
                    f.write("# %s\n" % text)
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return len(rows)

    def close(self):
        self.connection_pool.close_all()

# =============================================================================
# DIALOGS
# =============================================================================
class EntityDialog(Gtk.Dialog):
    def __init__(self, parent, title, fields):
        super().__init__(title=title, transient_for=parent, flags=0, modal=True)
        self.set_default_size(480, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        self._entries = {}
        for key, label, typ in fields:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_size_request(130, -1)
            row.pack_start(lbl, False, False, 0)
            if typ == bool:
                w = Gtk.ComboBoxText()
                w.append("0", "No")
                w.append("1", "Yes")
                w.set_active(0)
            elif typ == 'combo':
                w = Gtk.ComboBoxText()
            elif typ == 'combo_entry':
                w = Gtk.ComboBoxText.new_with_entry()
            elif typ == 'textview':
                sw = Gtk.ScrolledWindow()
                sw.set_size_request(-1, 80)
                tv = Gtk.TextView()
                tv.set_wrap_mode(Gtk.WrapMode.WORD)
                sw.add(tv)
                self._entries[f"{key}_tv"] = tv
                w = sw
            else:
                w = Gtk.Entry()
            row.pack_start(w, True, True, 0)
            box.pack_start(row, False, False, 0)
            self._entries[key] = w
        self.show_all()

    def get_val(self, key):
        w = self._entries.get(key)
        if isinstance(w, Gtk.Entry):
            return w.get_text()
        if isinstance(w, Gtk.ComboBoxText):
            if w.get_has_entry():
                # Prefer the entry text: users may type a value that is not in
                # the suggested list, and the widget keeps the last suggestion
                # active in that case.
                return (w.get_child().get_text() or "").strip()
            active_id = w.get_active_id()
            if active_id is not None:
                return active_id
            return w.get_active_text() or ""
        if isinstance(w, Gtk.ScrolledWindow):
            tv = self._entries.get(f"{key}_tv")
            if tv:
                b = tv.get_buffer()
                return b.get_text(b.get_start_iter(), b.get_end_iter(), True) or ""
        return ""

    def set_val(self, key, text):
        w = self._entries.get(key)
        if isinstance(w, Gtk.Entry):
            w.set_text(str(text if text is not None else ""))
        elif isinstance(w, Gtk.ComboBoxText):
            value = str(text if text is not None else "")
            if w.get_has_entry():
                w.get_child().set_text(value)
            elif w.get_active_id() != value:
                # Selection-only combos cannot display arbitrary text; only
                # apply values that actually exist in the model.
                model = w.get_model()
                if model is not None:
                    for row in model:
                        if row[0] == value or row[1] == value:
                            w.set_active_id(row[0])
                            break
        elif isinstance(w, Gtk.ScrolledWindow):
            tv = self._entries.get(f"{key}_tv")
            if tv:
                tv.get_buffer().set_text(str(text if text is not None else ""))

    def set_combo(self, key, items, active=None):
        w = self._entries.get(key)
        if not isinstance(w, Gtk.ComboBoxText):
            return
        items = [str(i) for i in items]
        # Editable combos should stay blank when the stored value is blank.
        if w.get_has_entry() and active == "":
            w.remove_all()
            for it in items:
                w.append(it, it)
            w.get_child().set_text("")
            return
        # Preserve values that are not part of the predefined list instead of
        # silently replacing saved user data with the first option.
        if active not in (None, "") and active not in items:
            if w.get_has_entry():
                w.remove_all()
                for it in items:
                    w.append(it, it)
                w.get_child().set_text(str(active))
                return
            items = [active] + items
        w.remove_all()
        for it in items:
            w.append(it, it)
        if active not in (None, "") and active in items:
            w.set_active_id(active)
        elif items:
            w.set_active(0)


class VesselDialog(EntityDialog):
    def __init__(self, parent, vessel=None, jurisdiction=None):
        fields = [
            ("name", "Vessel Name *", str), ("vessel_type", "Vessel Type", str),
            ("engine_make", "Engine Make", str), ("engine_model", "Engine Model", str),
            ("current_location", "Current Location", str), ("engine_hours", "Engine Hours", str),
            ("flag_state", "Flag State", 'combo_entry'),
            ("port_of_registry", "Port of Registry", 'combo_entry'),
            ("official_number", "Official Number", str), ("imo_number", "IMO Number", str),
            ("registration_expiry", "Registration Expiry (%s)" % date_input_hint(), str),
        ]
        super().__init__(parent, "Edit Vessel" if vessel and vessel.id else "Add Vessel",
                         fields)
        self._vid = vessel.id if vessel else None
        self._jurisdiction = jurisdiction
        flag_items = [jurisdiction.name] if jurisdiction else []
        port_items = []
        if jurisdiction:
            port_items = list(jurisdiction.ports_of_registry) + list(jurisdiction.common_ports)
        self.set_combo("flag_state", flag_items)
        self.set_combo("port_of_registry", port_items)
        if vessel:
            self.set_val("name", vessel.name)
            self.set_val("vessel_type", vessel.vessel_type)
            self.set_val("engine_make", vessel.engine_make)
            self.set_val("engine_model", vessel.engine_model)
            self.set_val("current_location", vessel.current_location)
            self.set_val("engine_hours", "%.1f" % (vessel.engine_hours or 0.0))
            self.set_combo("flag_state", flag_items, vessel.flag_state or "")
            self.set_combo("port_of_registry", port_items, vessel.port_of_registry or "")
            self.set_val("official_number", vessel.official_number)
            self.set_val("imo_number", vessel.imo_number)
            self.set_val("registration_expiry",
                         format_date(vessel.registration_expiry) if vessel.registration_expiry else "")
        else:
            # Pre-fill the jurisdiction's flag state for convenience; the user
            # can overwrite it for a foreign-flagged vessel.
            if jurisdiction:
                self._entries["flag_state"].get_child().set_text(jurisdiction.name)
            self.set_val("registration_expiry", "")

    def get_vessel(self):
        return Vessel(id=self._vid, name=self.get_val("name"), vessel_type=self.get_val("vessel_type"),
                      engine_make=self.get_val("engine_make"), engine_model=self.get_val("engine_model"),
                      current_location=self.get_val("current_location"),
                      engine_hours=DataValidator.to_float(self.get_val("engine_hours"), "Engine hours"),
                      flag_state=self.get_val("flag_state"),
                      port_of_registry=self.get_val("port_of_registry"),
                      official_number=self.get_val("official_number"),
                      imo_number=self.get_val("imo_number"),
                      registration_expiry=DataValidator.date_optional(
                          self.get_val("registration_expiry"), "Registration expiry"))


class MaintenanceDialog(EntityDialog):
    def __init__(self, parent, db, task=None):
        super().__init__(parent, "Edit Task" if task and task.id else "Add Maintenance Task",
                         [("vessel_id", "Vessel *", 'combo'), ("system", "System", str), ("task_name", "Task Name *", str),
                          ("description", "Description", 'textview'),
                          ("next_due", "Due (%s)" % date_input_hint(), str),
                          ("status", "Status", 'combo'), ("priority", "Priority", 'combo'),
                          ("interval_days", "Interval (days)", str), ("interval_hours", "Interval (hours)", str),
                          ("is_recurring", "Recurring", bool), ("cost", "Cost", str),
                          ("work_order", "Work Order (blank = auto)", str)])
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self.set_combo("status", ["pending", "in_progress", "completed", "overdue"])
        self.set_combo("priority", ["low", "medium", "high", "critical"])
        self._tid = task.id if task else None
        if task:
            self.set_combo("vessel_id", list(self._vmap.keys()), self._rvmap.get(task.vessel_id, ''))
            self.set_val("system", task.system)
            self.set_val("task_name", task.task_name)
            self.set_val("description", task.description)
            self.set_val("next_due", format_date(task.next_due) if task.next_due else "")
            self.set_combo("status", ["pending", "in_progress", "completed", "overdue"], task.status)
            self.set_combo("priority", ["low", "medium", "high", "critical"], task.priority)
            self.set_val("interval_days", str(task.interval_days) if task.interval_days else "")
            self.set_val("interval_hours", str(task.interval_hours) if task.interval_hours else "")
            self.set_combo("is_recurring", ["0", "1"], "1" if task.is_recurring else "0")
            self.set_val("cost", str(task.cost or ""))
            self.set_val("work_order", task.work_order or "")

    def get_task(self):
        vn = self.get_val("vessel_id")
        days = DataValidator.to_int(self.get_val("interval_days"), "Interval (days)") or None
        hours = DataValidator.to_float(self.get_val("interval_hours"), "Interval (hours)") or None
        cost = DataValidator.to_float(self.get_val("cost"), "Cost")
        return MaintenanceTask(id=self._tid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
                               system=self.get_val("system"), task_name=self.get_val("task_name"),
                               description=self.get_val("description"), next_due=self.get_val("next_due"),
                               status=self.get_val("status"), priority=self.get_val("priority"),
                               interval_days=days, interval_hours=hours,
                               is_recurring=self.get_val("is_recurring") == "1", cost=cost,
                               work_order=self.get_val("work_order"))


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
        cost = DataValidator.to_float(self._entries['cost'].get_text(), "Cost")
        if cost < 0:
            raise ValidationError("Cost cannot be negative")
        return (self._entries['completed_by'].get_text(), self._entries['notes'].get_text(), cost)


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
        hdr.set_markup("<b>Parts used by: %s</b>" % GLib.markup_escape_text(task_name or ""))
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
            self._part_store.append([p['id'], p.get('part_name', ''), p['quantity_used'], p['cost_per_unit'] or 0.0])

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
            label = "%s (stock: %d)" % (it['name'], it.get('current_stock') or 0)
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
                    qty = DataValidator.to_int(qty_entry.get_text() or 1, "Quantity", 1)
                    tp = TaskPart(task_id=self._task_id, item_id=item['id'], quantity_used=qty,
                                  cost_per_unit=item.get('unit_cost') or 0.0)
                    self._db.add_task_part(tp)
                    self._load_parts()
                except (ValidationError, DatabaseError) as e:
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
    def __init__(self, parent, db, item=None):
        super().__init__(parent, "Edit Item" if item else "Add Inventory Item",
                         [("name", "Item Name *", str), ("part_number", "Part Number", str),
                          ("category", "Category", str), ("current_stock", "Current Stock", str),
                          ("minimum_stock", "Minimum Stock", str), ("unit_cost", "Unit Cost", str),
                          ("supplier", "Supplier", 'combo')])
        self._iid = item['id'] if item else None
        suppliers = db.get_suppliers()
        self._supmap = {"— None —": 0}
        self._supidmap = {0: "— None —"}
        for s in suppliers:
            self._supmap[s['name']] = s['id']
            self._supidmap[s['id']] = s['name']
        self.set_combo("supplier", list(self._supmap.keys()))
        if item:
            for k in ('name', 'part_number', 'category'):
                self.set_val(k, item.get(k, ''))
            self.set_val("current_stock", str(item.get('current_stock', 0)))
            self.set_val("minimum_stock", str(item.get('minimum_stock', 0)))
            self.set_val("unit_cost", str(item.get('unit_cost', 0.0)))
            if item.get('supplier_id'):
                self.set_combo("supplier", list(self._supmap.keys()), self._supidmap.get(item['supplier_id'], '— None —'))

    def get_data(self):
        return {'id': self._iid, 'name': self.get_val("name"), 'part_number': self.get_val("part_number"),
                'category': self.get_val("category"),
                'current_stock': DataValidator.to_int(self.get_val("current_stock"), "Current stock"),
                'minimum_stock': DataValidator.to_int(self.get_val("minimum_stock"), "Minimum stock"),
                'unit_cost': DataValidator.to_float(self.get_val("unit_cost"), "Unit cost"),
                'supplier_id': self._supmap.get(self.get_val("supplier"), 0)}


class StockAdjustDialog(Gtk.Dialog):
    def __init__(self, parent, item_name, current_stock):
        super().__init__(title=f"Adjust Stock: {item_name}", transient_for=parent, flags=0, modal=True)
        self.set_default_size(350, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Apply", Gtk.ResponseType.OK)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        box.pack_start(Gtk.Label(label=f"Current stock: {current_stock}"), False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(Gtk.Label(label="Change (+/-):"), False, False, 0)
        self._entry = Gtk.Entry()
        self._entry.set_width_chars(10)
        row.pack_start(self._entry, False, False, 0)
        box.pack_start(row, False, False, 0)
        self._note_entry = Gtk.Entry()
        self._note_entry.set_placeholder_text("Reason (optional)")
        box.pack_start(self._note_entry, False, False, 0)
        h = Gtk.Label(label="Use positive to add stock, negative to remove")
        h.set_xalign(0)
        h.get_style_context().add_class("dim-label")
        box.pack_start(h, False, False, 0)
        self.show_all()

    def get_delta(self):
        try:
            return int(self._entry.get_text())
        except ValueError:
            raise ValidationError("Enter a valid integer (+/-)")

    def get_note(self):
        return self._note_entry.get_text()


class ExpenseDialog(EntityDialog):
    CATEGORIES = ["Fuel", "Maintenance", "Supplies", "Dockage", "Crew", "Insurance", "Registration",
                  "Equipment", "Repairs", "Food", "Utilities", "Miscellaneous"]
    TAX_CATEGORIES = ["", "GST", "VAT", "Sales Tax", "Excise", "Luxury Tax", "ABST", "GCT", "BTW", "TVA"]
    CURRENCIES = ["TTD", "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "XCD", "BBD", "JMD",
                  "BZD", "GYD", "HTG", "SRD", "BSD", "BMD", "KYD"]

    def __init__(self, parent, db, expense=None, categories=None, currencies=None,
                 default_currency=None, default_tax_rate=None, tax_regime=None):
        tax_label = tax_regime or "GST/VAT"
        super().__init__(parent, "Edit Expense" if expense and expense.id else "Add Expense",
                         [("vessel_id", "Vessel", "combo"), ("category", "Category *", "combo"),
                          ("amount", "Amount *", str), ("currency", "Currency", "combo"),
                          ("expense_date", "Date (%s) *" % date_input_hint(), str),
                          ("description", "Description", "textview"), ("vendor", "Vendor", str),
                          ("tax_deductible", "Tax Deductible", bool),
                          ("tax_category", "Tax Type", "combo"),
                          ("gst_rate", "%s rate (%%)" % tax_label, str)])
        self._db = db
        self._categories = list(dict.fromkeys((categories or []) + self.CATEGORIES))
        self._currencies = list(dict.fromkeys((currencies or []) + self.CURRENCIES))
        # Include the active jurisdiction's tax name (for example ABST, GCT or
        # TVA) in the Tax Type list so the preset can be selected directly.
        self._tax_categories = list(dict.fromkeys(
            self.TAX_CATEGORIES + ([tax_regime] if tax_regime else [])))
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._vmap["— None —"] = 0
        self._rvmap = {v['id']: v['name'] for v in vessels}
        items = list(self._vmap.keys())
        self.set_combo("vessel_id", items)
        self.set_combo("category", self._categories)
        self.set_combo("currency", self._currencies)
        self.set_combo("tax_category", self._tax_categories)
        if expense is None and tax_regime and (default_tax_rate or 0) > 0:
            # New expenses default to the jurisdiction's tax type; a zero-rate
            # ruleset deliberately leaves this blank.
            self.set_combo("tax_category", self._tax_categories, tax_regime)
        if default_tax_rate is not None:
            default_gst = str(default_tax_rate)
        else:
            default_gst = db.get_setting("gst_rate", "0") or "0"
        self.set_val("gst_rate", default_gst)
        if default_currency:
            self.set_combo("currency", self._currencies, default_currency)
        # Trip selection
        self._trip_combo = Gtk.ComboBoxText()
        self._trip_combo.append("0", "None")
        self._trip_map = {}
        for t in db.get_trips():
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
            self.set_combo("vessel_id", items, self._rvmap.get(expense.vessel_id, "— None —"))
            self.set_combo("category", self._categories, expense.category)
            self.set_val("amount", str(expense.amount))
            self.set_val("expense_date", format_date(expense.expense_date))
            self.set_combo("currency", self._currencies, expense.currency or (default_currency or DEFAULT_CURRENCY))
            self.set_val("description", expense.description)
            self.set_val("vendor", expense.vendor)
            self.set_combo("tax_deductible", ["0", "1"], "1" if expense.tax_deductible else "0")
            self.set_combo("tax_category", self._tax_categories, expense.tax_category or "")
            self.set_val("gst_rate", str(expense.gst_rate or 0.0))
            if expense.trip_id:
                self._trip_combo.set_active_id(str(expense.trip_id))

        self.show_all()

    def get_expense(self):
        vn = self.get_val("vessel_id")
        trip_id = int(self._trip_combo.get_active_id() or 0)
        amount = DataValidator.to_float(self.get_val("amount"), "Amount")
        gst_rate = DataValidator.to_float(self.get_val("gst_rate"), "GST/VAT rate")
        return Expense(
            id=self._eid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
            trip_id=trip_id if trip_id > 0 else None,
            category=self.get_val("category"), amount=amount,
            currency=self.get_val("currency") or DEFAULT_CURRENCY,
            expense_date=DataValidator.date_optional(self.get_val("expense_date"), "Expense date"),
            description=self.get_val("description"),
            vendor=self.get_val("vendor"), tax_deductible=self.get_val("tax_deductible") == "1",
            tax_category=self.get_val("tax_category"), gst_rate=gst_rate)


class TripDialog(EntityDialog):
    def __init__(self, parent, db, trip=None):
        super().__init__(parent, "Edit Trip" if trip and trip.id else "Add Trip Log",
                         [("vessel_id", "Vessel *", 'combo'), ("departure", "Departure", str),
                          ("arrival", "Arrival", str), ("destination", "Destination", str),
                          ("distance", "Distance (nm)", str), ("fuel_used", "Fuel Used", str)])
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self._tid = trip.id if trip else None
        if trip:
            self.set_combo("vessel_id", list(self._vmap.keys()), self._rvmap.get(trip.vessel_id, ''))
            self.set_val("departure", trip.departure)
            self.set_val("arrival", trip.arrival)
            self.set_val("destination", trip.destination)
            self.set_val("distance", str(trip.distance))
            self.set_val("fuel_used", str(trip.fuel_used))

    def get_trip(self):
        vn = self.get_val("vessel_id")
        distance = DataValidator.to_float(self.get_val("distance"), "Distance")
        fuel = DataValidator.to_float(self.get_val("fuel_used"), "Fuel used")
        if distance < 0 or fuel < 0:
            raise ValidationError("Distance and fuel used cannot be negative")
        return TripLog(id=self._tid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
                       departure=self.get_val("departure"), arrival=self.get_val("arrival"),
                       destination=self.get_val("destination"), distance=distance, fuel_used=fuel)


class SupplierDialog(EntityDialog):
    def __init__(self, parent, supplier=None):
        super().__init__(parent, "Edit Supplier" if supplier and supplier.id else "Add Supplier",
                         [("name", "Supplier Name *", str), ("contact", "Contact Person", str),
                          ("phone", "Phone", str), ("email", "Email", str), ("preferred", "Preferred", bool)])
        self._sid = supplier.id if supplier else None
        if supplier:
            self.set_val("name", supplier.name)
            self.set_val("contact", supplier.contact)
            self.set_val("phone", supplier.phone)
            self.set_val("email", supplier.email)
            self.set_combo("preferred", ["0", "1"], "1" if supplier.preferred else "0")

    def get_supplier(self):
        return Supplier(id=self._sid, name=self.get_val("name"), contact=self.get_val("contact"),
                        phone=self.get_val("phone"), email=self.get_val("email"),
                        preferred=self.get_val("preferred") == "1")


class CrewDialog(EntityDialog):
    ROLES = ["Captain", "First Mate", "Engineer", "Deckhand", "Cook", "Crew", "Purser", "Engine Cadet",
             "Deck Cadet", "Electrician", "Bosun"]
    STATUSES = ["active", "inactive", "terminated"]

    def __init__(self, parent, db, crew_member=None):
        super().__init__(parent, "Edit Crew Member" if crew_member and crew_member.id else "Add Crew Member",
                         [("first_name", "First Name *", str), ("last_name", "Last Name *", str),
                          ("role", "Role *", "combo"), ("status", "Status", "combo"),
                          ("email", "Email", str), ("phone", "Phone", str),
                          ("emergency_contact", "Emergency Contact", str), ("emergency_phone", "Emergency Phone", str),
                          ("date_joined", "Date Joined (%s)" % date_input_hint(), str),
                          ("date_left", "Date Left (%s)" % date_input_hint(), str)])
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
            self.set_val("date_joined", format_date(crew_member.date_joined) if crew_member.date_joined else "")
            self.set_val("date_left", format_date(crew_member.date_left) if crew_member.date_left else "")

    def get_crew_member(self):
        return CrewMember(id=self._cid, first_name=self.get_val("first_name"), last_name=self.get_val("last_name"),
                          role=self.get_val("role"), status=self.get_val("status"),
                          email=self.get_val("email"), phone=self.get_val("phone"),
                          emergency_contact=self.get_val("emergency_contact"),
                          emergency_phone=self.get_val("emergency_phone"),
                          date_joined=self.get_val("date_joined"),
                          date_left=self.get_val("date_left"))


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
            self.set_val("interval_days", str(template.interval_days) if template.interval_days else "")
            self.set_val("interval_hours", str(template.interval_hours) if template.interval_hours else "")
            self.set_val("default_vessel_type", template.default_vessel_type)

    def get_template(self):
        days = DataValidator.to_int(self.get_val("interval_days"), "Interval (days)") or None
        hours = DataValidator.to_float(self.get_val("interval_hours"), "Interval (hours)") or None
        return TaskTemplate(id=self._tid, name=self.get_val("name"), system=self.get_val("system"),
                            description=self.get_val("description"), priority=self.get_val("priority"),
                            interval_days=days, interval_hours=hours,
                            default_vessel_type=self.get_val("default_vessel_type"))


class CertDialog(Gtk.Dialog):
    def __init__(self, parent, db, crew_id, crew_name, presets=None, categories=None,
                 default_issuing_body="", on_saved=None):
        super().__init__(title="Certifications - %s" % crew_name, transient_for=parent, flags=0, modal=True)
        self.set_default_size(700, 450)
        self._db = db
        self._crew_id = crew_id
        self._presets = presets or []
        self._categories = categories or []
        self._default_issuing_body = default_issuing_body
        self._on_saved = on_saved
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)

        hdr = Gtk.Label()
        hdr.set_markup("<b>Certifications for %s</b>" % GLib.markup_escape_text(crew_name or ""))
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
                cert.get('cert_number', ''), format_date(cert.get('issue_date')),
                format_date(cert.get('expiry_date'))
            ])

    def _add_cert(self):
        d = CertEntryDialog(self, self._db, self._crew_id, presets=self._presets,
                            categories=self._categories,
                            default_issuing_body=self._default_issuing_body)
        if d.run() == Gtk.ResponseType.OK:
            try:
                cert = d.apply()
                self._load_certs()
                if self._on_saved is not None and cert is not None:
                    try:
                        self._on_saved(cert)
                    except Exception as e:
                        logger.warning("Post-save certification check failed: %s", e)
            except (ValidationError, DatabaseError):
                pass
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

    def __init__(self, parent, db, crew_id, presets=None, categories=None, default_issuing_body=""):
        self._presets = [dict(p) for p in (presets or []) if p.get("name")]
        cats = list(dict.fromkeys((categories or []) + self.CATEGORIES))
        super().__init__(parent, "Add Certification",
                         [("name", "Certification Name *", 'combo_entry'),
                          ("issuing_body", "Issuing Body", str),
                          ("cert_number", "Certificate Number", str), ("category", "Category", "combo"),
                          ("issue_date", "Issue Date (%s)" % date_input_hint(), str),
                          ("expiry_date", "Expiry Date (%s)" % date_input_hint(), str)])
        self._db = db
        self._crew_id = crew_id
        self.set_combo("category", cats)
        self.set_combo("name", [p["name"] for p in self._presets], "")
        if self._presets:
            preset_combo = Gtk.ComboBoxText()
            preset_combo.append("", "Choose a suggested certificate…")
            for i, p in enumerate(self._presets):
                preset_combo.append(str(i), p["name"])
            preset_combo.set_active(0)

            def apply_preset(widget):
                idx = widget.get_active_id()
                if idx in (None, ""):
                    return
                try:
                    preset = self._presets[int(idx)]
                except (IndexError, ValueError):
                    return
                self.set_val("name", preset["name"])
                if preset.get("issuing_body"):
                    self.set_val("issuing_body", preset["issuing_body"])
                if preset.get("category"):
                    self.set_combo("category", cats, preset["category"])

            preset_combo.connect("changed", apply_preset)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label="Suggested:", xalign=0), False, False, 0)
            row.pack_start(preset_combo, True, True, 0)
            self.get_content_area().pack_start(row, False, False, 0)
        if default_issuing_body:
            self.set_val("issuing_body", default_issuing_body)

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
                          ("provider", "Provider", str),
                          ("date_completed", "Date Completed (%s)" % date_input_hint(), str),
                          ("expiry_date", "Expiry Date (%s)" % date_input_hint(), str),
                          ("cost", "Cost", str), ("notes", "Notes", "textview")])
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
            self.set_val("date_completed", format_date(record.date_completed) if record.date_completed else "")
            self.set_val("expiry_date", format_date(record.expiry_date) if record.expiry_date else "")
            self.set_val("cost", str(record.cost or ""))
            self.set_val("notes", record.notes)

    def get_training_record(self):
        cname = self.get_val("crew_member_id")
        cid = self._cmap.get(cname, 0)
        cost = DataValidator.to_float(self.get_val("cost"), "Cost")
        return TrainingRecord(id=self._tid, crew_member_id=cid, course_name=self.get_val("course_name"),
                              provider=self.get_val("provider"), date_completed=self.get_val("date_completed"),
                              expiry_date=self.get_val("expiry_date"), cost=cost, notes=self.get_val("notes"))


class WatchScheduleDialog(EntityDialog):
    ROLES = ["OOW", "Chief Officer", "Second Officer", "Third Officer", "Lookout", "Helmsman",
             "Engineering Watch", "Electrician Watch", "Deck Watch", "Safety Officer"]

    def __init__(self, parent, db, schedule=None):
        super().__init__(parent, "Edit Watch Schedule" if schedule and schedule.id else "Add Watch Schedule",
                         [("crew_member_id", "Crew Member *", "combo"), ("vessel_id", "Vessel", "combo"),
                          ("date", "Date (%s)" % date_input_hint(), str),
                          ("start_time", "Start (HH:MM)", str),
                          ("end_time", "End (HH:MM)", str), ("role_on_watch", "Role", "combo"),
                          ("notes", "Notes", "textview")])
        self._db = db
        crew = db.get_crew_members()
        self._cmap = {("%s %s" % (c['first_name'], c['last_name'])): c['id'] for c in crew}
        self._rcmap = {v: k for k, v in self._cmap.items()}
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._vmap["— None —"] = 0
        self._rvmap = {v['id']: v['name'] for v in vessels}
        items = list(self._vmap.keys())
        self.set_combo("crew_member_id", list(self._cmap.keys()))
        self.set_combo("vessel_id", items)
        self.set_combo("role_on_watch", self.ROLES)
        self._wid = schedule.id if schedule else None
        if schedule:
            cname = self._rcmap.get(schedule.crew_member_id, "")
            self.set_combo("crew_member_id", list(self._cmap.keys()), cname)
            self.set_combo("vessel_id", items, self._rvmap.get(schedule.vessel_id, "— None —"))
            self.set_val("date", format_date(schedule.date) if schedule.date else "")
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


class FuelLogDialog(EntityDialog):
    FUEL_TYPES = ["Diesel", "Marine Diesel", "Gasoline", "Petrol", "CNG", "Electric", "Other"]

    def __init__(self, parent, db, fuel_log=None):
        super().__init__(parent, "Edit Fuel Log" if fuel_log and fuel_log.id else "Add Fuel Log",
                         [("vessel_id", "Vessel *", "combo"), ("gallons", "Gallons *", str),
                          ("cost_per_gallon", "Cost per gallon", str), ("total_cost", "Total cost (optional)", str),
                          ("fuel_type", "Fuel type", "combo"), ("vendor", "Vendor", str),
                          ("location", "Location", str), ("engine_hours", "Engine hours", str)])
        self._db = db
        self._fid = fuel_log.id if fuel_log else None
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self.set_combo("fuel_type", self.FUEL_TYPES)

        self._trip_combo = Gtk.ComboBoxText()
        self._trip_combo.append("0", "None")
        for t in db.get_trips():
            lbl = "%s - %s" % (t.get('destination', '?'), t.get('departure', ''))
            self._trip_combo.append(str(t['id']), lbl)
        self._trip_combo.set_active(0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(Gtk.Label(label="Trip:", xalign=0), False, False, 0)
        row.pack_start(self._trip_combo, True, True, 0)
        self.get_content_area().pack_start(row, False, False, 0)

        if fuel_log:
            self.set_combo("vessel_id", list(self._vmap.keys()), self._rvmap.get(fuel_log.vessel_id, ''))
            self.set_val("gallons", str(fuel_log.gallons))
            self.set_val("cost_per_gallon", str(fuel_log.cost_per_gallon or ""))
            self.set_val("total_cost", str(fuel_log.total_cost or ""))
            self.set_combo("fuel_type", self.FUEL_TYPES, fuel_log.fuel_type or "Diesel")
            self.set_val("vendor", fuel_log.vendor)
            self.set_val("location", fuel_log.location)
            self.set_val("engine_hours", str(fuel_log.engine_hours or ""))
            if fuel_log.trip_id:
                self._trip_combo.set_active_id(str(fuel_log.trip_id))
        self.show_all()

    def get_fuel_log(self):
        vn = self.get_val("vessel_id")
        trip_id = int(self._trip_combo.get_active_id() or 0)
        return FuelLog(id=self._fid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
                       trip_id=trip_id if trip_id > 0 else None,
                       gallons=DataValidator.to_float(self.get_val("gallons"), "Gallons"),
                       cost_per_gallon=DataValidator.to_float(self.get_val("cost_per_gallon"), "Cost per gallon"),
                       total_cost=DataValidator.to_float(self.get_val("total_cost"), "Total cost"),
                       fuel_type=self.get_val("fuel_type"), vendor=self.get_val("vendor"),
                       location=self.get_val("location"),
                       engine_hours=DataValidator.to_float(self.get_val("engine_hours"), "Engine hours"))


class BudgetDialog(EntityDialog):
    def __init__(self, parent, db, budget=None, categories=None, currency=None):
        super().__init__(parent, "Edit Budget" if budget and budget.id else "Add Budget",
                         [("vessel_id", "Vessel *", "combo"), ("fiscal_year", "Fiscal Year *", str),
                          ("category", "Category *", "combo"),
                          ("budget_amount", "Budget Amount (%s) *" % (currency or _format_state["currency"]), str),
                          ("notes", "Notes", "textview")])
        self._db = db
        self._bid = budget.id if budget else None
        self._categories = list(dict.fromkeys((categories or []) + ExpenseDialog.CATEGORIES))
        vessels = db.get_vessels()
        self._vmap = {v['name']: v['id'] for v in vessels}
        self._rvmap = {v['id']: v['name'] for v in vessels}
        self.set_combo("vessel_id", list(self._vmap.keys()))
        self.set_combo("category", self._categories)
        self.set_val("fiscal_year", str(budget.fiscal_year if budget else datetime.datetime.now().year))
        if budget:
            self.set_combo("vessel_id", list(self._vmap.keys()), self._rvmap.get(budget.vessel_id, ''))
            self.set_combo("category", self._categories, budget.category)
            self.set_val("budget_amount", str(budget.budget_amount))
            self.set_val("notes", budget.notes)

    def get_budget(self):
        vn = self.get_val("vessel_id")
        return Budget(id=self._bid, vessel_id=self._vmap.get(vn, 0), vessel_name=vn,
                      fiscal_year=DataValidator.to_int(self.get_val("fiscal_year"), "Fiscal year",
                                                       datetime.datetime.now().year),
                      category=self.get_val("category"),
                      budget_amount=DataValidator.to_float(self.get_val("budget_amount"), "Budget amount"),
                      notes=self.get_val("notes"))


class PreferencesDialog(Gtk.Dialog):
    def __init__(self, parent, config, db, registry=None):
        super().__init__(title="Preferences", transient_for=parent, flags=0, modal=True)
        self.set_default_size(640, 560)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self._config = config
        self._db = db
        self._registry = registry if registry is not None else _load_jurisdiction_registry(
            allow_custom=bool(config.get("allow_custom_rulesets", True)))
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        notebook = Gtk.Notebook()
        self._entries = {}

        def add_entry(tab, key, label, default, typ='str'):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_size_request(210, -1)
            row.pack_start(lbl, False, False, 0)
            if typ == 'combo':
                w = Gtk.ComboBoxText()
                for val in ("No", "Yes"):
                    w.append(val, val)
                w.set_active(0 if not default else 1)
            else:
                w = Gtk.Entry()
                w.set_text(str(default))
                w.set_width_chars(18)
            row.pack_start(w, False, False, 0)
            tab.pack_start(row, False, False, 0)
            self._entries[key] = w

        # General tab
        gen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gen.set_border_width(12)
        add_entry(gen, "due_soon_days", "Due-soon threshold (days)", config.get("due_soon_days", 7))
        add_entry(gen, "backup_retention_days", "Backup retention (days)", config.get("backup_retention_days", 30))
        add_entry(gen, "backup_interval_hours", "Backup interval (hours)", config.get("backup_interval_hours", 24))
        add_entry(gen, "auto_backup", "Auto backup", config.get("auto_backup", True), 'combo')
        add_entry(gen, "data_retention_days", "Data retention (days)", config.get("data_retention_days", 730))
        add_entry(gen, "prune_old_data", "Auto-prune old records", config.get("prune_old_data", False), 'combo')
        note = Gtk.Label(label="Auto-pruning permanently deletes expenses, trips and fuel logs\nolder than the retention period at startup. Off by default.")
        note.set_xalign(0)
        note.get_style_context().add_class("dim-label")
        gen.pack_start(note, False, False, 0)
        notebook.append_page(gen, Gtk.Label(label="General"))

        # Security tab
        sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sec.set_border_width(12)
        sec_lbl = Gtk.Label()
        sec_lbl.set_markup("<b>Security &amp; Compliance Settings</b>")
        sec.pack_start(sec_lbl, False, False, 0)
        add_entry(sec, "session_timeout_minutes", "Session timeout (minutes, 0=off)", config.get("session_timeout_minutes", 30))
        add_entry(sec, "audit_retention_days", "Audit log retention (days)", config.get("audit_retention_days", 365))
        add_entry(sec, "require_pin", "Require PIN to open", config.get("require_pin", False), 'combo')
        self._pin_entry = Gtk.Entry()
        self._pin_entry.set_placeholder_text("New PIN (4-8 digits, leave blank to keep)")
        self._pin_entry.set_visibility(False)
        self._pin_entry.set_width_chars(18)
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
        add_entry(fin, "currency", "Default currency", config.get("currency", DEFAULT_CURRENCY))
        add_entry(fin, "gst_rate", "Default tax rate (%)", config.get("gst_rate", DEFAULT_TAX_RATE))
        add_entry(fin, "fiscal_year_start", "Fiscal year start (MM-DD)", config.get("fiscal_year_start", "01-01"))
        add_entry(fin, "company_name", "Company name", config.get("company_name", ""))
        tax_note = Gtk.Label(label="The tax rate is an editable preset, not a fixed rule. "
                                    "Verify the current rate with the authority named on the "
                                    "Jurisdiction tab.")
        tax_note.set_xalign(0)
        tax_note.set_line_wrap(True)
        tax_note.get_style_context().add_class("dim-label")
        fin.pack_start(tax_note, False, False, 0)
        notebook.append_page(fin, Gtk.Label(label="Financial"))

        # Jurisdiction tab
        jur = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        jur.set_border_width(12)
        jur_lbl = Gtk.Label()
        jur_lbl.set_markup("<b>Jurisdiction &amp; Compliance Guard Rails</b>")
        jur_lbl.set_xalign(0)
        jur.pack_start(jur_lbl, False, False, 0)
        jur_combo = Gtk.ComboBoxText()
        self._jurisdiction_ids = []
        rulesets = self._registry.list() if self._registry else []
        active_id = str(config.get("jurisdiction_preset", DEFAULT_JURISDICTION) or
                        DEFAULT_JURISDICTION)
        active_idx = 0
        for i, rs in enumerate(rulesets):
            label = "%s (%s)" % (rs.name, rs.country_code)
            if getattr(rs, "is_custom", False):
                label += " — custom"
            jur_combo.append(rs.id, label)
            self._jurisdiction_ids.append(rs.id)
            if rs.id == active_id:
                active_idx = i
        if rulesets:
            jur_combo.set_active(active_idx)
        jur_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        jur_row.pack_start(Gtk.Label(label="Nation / ruleset:", xalign=0), False, False, 0)
        jur_row.pack_start(jur_combo, True, True, 0)
        reload_btn = Gtk.Button(label="Reload")
        reload_btn.set_tooltip_text("Reload custom rulesets from the configuration directory")
        reload_btn.connect("clicked", lambda w: self._reload_rulesets())
        jur_row.pack_start(reload_btn, False, False, 0)
        jur.pack_start(jur_row, False, False, 0)

        self._jur_combo = jur_combo
        self._jur_info = Gtk.Label()
        self._jur_info.set_xalign(0)
        self._jur_info.set_line_wrap(True)
        self._jur_info.set_selectable(True)
        info_frame = Gtk.Frame()
        info_sw = Gtk.ScrolledWindow()
        info_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        info_sw.set_size_request(-1, 130)
        info_sw.add(self._jur_info)
        info_frame.add(info_sw)
        jur.pack_start(info_frame, True, True, 0)

        self._jur_apply = Gtk.CheckButton(
            label="Apply this nation's presets (currency, locale, date format and tax rate)")
        self._saved_currency = str(config.get("currency", DEFAULT_CURRENCY) or DEFAULT_CURRENCY)
        try:
            self._saved_gst = float(config.get("gst_rate", DEFAULT_TAX_RATE) or 0.0)
        except (TypeError, ValueError):
            self._saved_gst = float(DEFAULT_TAX_RATE)
        # Only offer to overwrite formats when the saved settings already match
        # the selected ruleset. Otherwise opening Preferences to change an
        # unrelated option could silently reset a custom currency or tax rate.
        active_ruleset = self._selected_ruleset()
        presets_match = active_ruleset is not None and (
            self._saved_currency.upper() == str(active_ruleset.currency).upper()
            and abs(self._saved_gst - float(active_ruleset.tax_rate or 0.0)) < 1e-9
            and str(config.get("locale", DEFAULT_LOCALE)) == active_ruleset.locale
            and str(config.get("date_format", DEFAULT_DATE_FORMAT)) == active_ruleset.date_format
        )
        self._jur_apply.set_active(bool(presets_match))
        self._jur_apply.connect("toggled", self._on_apply_toggled)
        jur.pack_start(self._jur_apply, False, False, 0)
        self._jur_guardrails = Gtk.CheckButton(
            label="Show advisory guard-rail warnings after saving records")
        self._jur_guardrails.set_active(bool(config.get("show_guardrails", True)))
        jur.pack_start(self._jur_guardrails, False, False, 0)
        self._jur_verify = Gtk.CheckButton(
            label="Include a reminder to verify the tax rate with the authority")
        self._jur_verify.set_active(bool(config.get("verify_tax_rate", True)))
        jur.pack_start(self._jur_verify, False, False, 0)
        self._jur_custom = Gtk.CheckButton(
            label="Load custom rulesets from the configuration directory "
                  "(data-only JSON, never executed)")
        self._jur_custom.set_active(bool(config.get("allow_custom_rulesets", True)))
        jur.pack_start(self._jur_custom, False, False, 0)
        disclaimer = Gtk.Label()
        disclaimer.set_markup(
            "<i>These presets are informational guard rails only and are not legal advice. "
            "Tax rates, fees and certificate requirements change: verify them with the named "
            "authority before relying on them.</i>")
        disclaimer.set_xalign(0)
        disclaimer.set_line_wrap(True)
        disclaimer.get_style_context().add_class("dim-label")
        jur.pack_start(disclaimer, False, False, 0)
        notebook.append_page(jur, Gtk.Label(label="Jurisdiction"))

        jur_combo.connect("changed", self._on_jurisdiction_changed)
        self._on_jurisdiction_changed(jur_combo)

        # Storage tab
        storage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        storage.set_border_width(12)
        backup_btn = Gtk.Button(label="Show Backup Location")
        backup_btn.connect("clicked", lambda x: self._show_info())
        storage.pack_start(backup_btn, False, False, 0)
        audit_btn = Gtk.Button(label="Show Data Directory")
        audit_btn.connect("clicked", lambda x: self._show_data_dir())
        storage.pack_start(audit_btn, False, False, 0)
        notebook.append_page(storage, Gtk.Label(label="Storage"))
        box.pack_start(notebook, True, True, 0)
        self.show_all()

    def _selected_ruleset(self):
        if self._registry is None:
            return None
        idx = self._jur_combo.get_active()
        if idx is None or idx < 0 or idx >= len(self._jurisdiction_ids):
            return None
        return self._registry.get(self._jurisdiction_ids[idx])

    def _on_apply_toggled(self, button):
        """Keep the saved formats when the user declines the selected presets."""
        if button.get_active():
            self._on_jurisdiction_changed(self._jur_combo)
            return
        self._entries["currency"].set_text(self._saved_currency)
        self._entries["gst_rate"].set_text("%.2f" % self._saved_gst)

    def _on_jurisdiction_changed(self, combo):
        rs = self._selected_ruleset()
        if rs is None:
            self._jur_info.set_text("No jurisdiction ruleset is available.")
            return
        lines = ["<b>%s (%s)</b>" % (GLib.markup_escape_text(rs.name),
                                     GLib.markup_escape_text(rs.country_code))]
        status = getattr(rs, "caricom_status", "none")
        lines.append("CARICOM status: %s · Locale: %s · Currency: %s · "
                     "Date format: %s"
                     % (GLib.markup_escape_text(str(status).title()),
                        GLib.markup_escape_text(rs.locale),
                        GLib.markup_escape_text(rs.currency),
                        GLib.markup_escape_text(rs.date_format)))
        tax_rate = getattr(rs, "tax_rate", 0.0)
        tax_auth = getattr(rs, "tax_authority", "")
        if float(tax_rate or 0.0) > 0:
            tax_text = "Tax preset: %s%s" % (
                GLib.markup_escape_text(tax_preset_label(rs, tax_rate)),
                (" — " + GLib.markup_escape_text(tax_auth)) if tax_auth else "")
        else:
            tax_text = ("Tax preset: %s; confirm the tax treatment with %s"
                        % (GLib.markup_escape_text(tax_preset_label(rs, tax_rate)),
                           GLib.markup_escape_text(tax_auth or "the tax authority")))
        lines.append(tax_text)
        if rs.authorities:
            lines.append("<b>Authorities to verify with</b>")
            for auth in rs.authorities[:6]:
                role = auth.get("role", "")
                lines.append("• %s%s" % (GLib.markup_escape_text(auth.get("name", "")),
                                         (" — " + GLib.markup_escape_text(role)) if role else ""))
        lines.append("Ruleset version %s · Last reviewed %s"
                     % (GLib.markup_escape_text(rs.ruleset_version or "1.0.0"),
                        GLib.markup_escape_text(rs.updated_at or "unknown")))
        if getattr(rs, "is_custom", False):
            lines.append("<i>Custom ruleset loaded from the configuration directory.</i>")
        if self._registry is not None and self._registry.errors:
            lines.append("<i>%d custom ruleset file(s) were ignored; see the log for details.</i>"
                         % len(self._registry.errors))
        lines.append("<i>%s</i>" % GLib.markup_escape_text(rs.disclaimer))
        self._jur_info.set_markup("\n".join(lines))
        if self._jur_apply.get_active():
            self._entries["currency"].set_text(rs.currency)
            self._entries["gst_rate"].set_text("%.2f" % float(getattr(rs, "tax_rate", 0.0) or 0.0))

    def _reload_rulesets(self):
        if self._registry is None:
            show_error(self, "Jurisdiction rulesets are not available in this installation.")
            return
        # Resolve the selection before the registry changes; the combo indices
        # refer to the previous ruleset list.
        current = self._selected_ruleset()
        current_id = current.id if current is not None else DEFAULT_JURISDICTION
        try:
            self._registry.reload(allow_custom=self._jur_custom.get_active())
        except Exception as e:
            show_error(self, "Could not reload rulesets: %s" % e)
            return
        self._jur_combo.remove_all()
        self._jurisdiction_ids = []
        active_idx = 0
        for i, rs in enumerate(self._registry.list()):
            label = "%s (%s)" % (rs.name, rs.country_code)
            if getattr(rs, "is_custom", False):
                label += " — custom"
            self._jur_combo.append(rs.id, label)
            self._jurisdiction_ids.append(rs.id)
            if rs.id == current_id:
                active_idx = i
        if self._jurisdiction_ids:
            self._jur_combo.set_active(active_idx)
        if self._registry.errors:
            show_error(self, "Some custom rulesets were ignored:\n\n"
                             + "\n".join(self._registry.errors[:8]))
        else:
            self._info("Rulesets reloaded", "%d rulesets available."
                       % len(self._jurisdiction_ids))

    def _info(self, title, msg):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(msg)
        d.run()
        d.destroy()

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
            due_soon = max(1, DataValidator.to_int(self._entries["due_soon_days"].get_text(), "Due-soon threshold", 7))
            backup_days = max(1, DataValidator.to_int(self._entries["backup_retention_days"].get_text(), "Backup retention", 30))
            backup_hours = max(1, DataValidator.to_int(self._entries["backup_interval_hours"].get_text(), "Backup interval", 24))
            data_days = max(1, DataValidator.to_int(self._entries["data_retention_days"].get_text(), "Data retention", 730))
            session_min = DataValidator.to_int(self._entries["session_timeout_minutes"].get_text(), "Session timeout", 30)
            audit_days = max(1, DataValidator.to_int(self._entries["audit_retention_days"].get_text(), "Audit retention", 365))
            gst = DataValidator.to_float(self._entries["gst_rate"].get_text(), "Tax rate")
            if gst < 0 or gst > 100:
                raise ValidationError("Tax rate must be between 0 and 100")
            currency = (self._entries["currency"].get_text().strip() or DEFAULT_CURRENCY).upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValidationError("Currency must be a three-letter ISO code (for example TTD)")
        except ValidationError as e:
            show_error(self, str(e))
            return False

        require_pin = self._entries["require_pin"].get_active_text() == "Yes"
        pin = self._pin_entry.get_text().strip()
        if pin:
            if len(pin) < 4 or len(pin) > 8 or not pin.isdigit():
                show_error(self, "PIN must be 4-8 digits")
                return False
            self._config.set("pin_hash", hash_pin(pin))
        elif require_pin and not self._config.get("pin_hash"):
            show_error(self, "Set a PIN before enabling PIN protection")
            return False

        # Jurisdiction selection and guard-rail toggles.
        ruleset = self._selected_ruleset()
        if (ruleset is not None and getattr(ruleset, "is_custom", False)
                and not self._jur_custom.get_active()):
            # The custom file will not be loaded after this save; fall back to
            # the built-in default instead of storing a ruleset id that will
            # disappear on the next reload.
            ruleset = (self._registry.require(DEFAULT_JURISDICTION)
                       if self._registry is not None else None)
        apply_presets = self._jur_apply.get_active() and ruleset is not None
        if apply_presets:
            defaults = ruleset.get_defaults()
            self._config.set("country", defaults.get("country", ruleset.country_code))
            self._config.set("locale", defaults.get("locale", ruleset.locale))
            self._config.set("timezone", defaults.get("timezone", ruleset.timezone))
            self._config.set("date_format", defaults.get("date_format", ruleset.date_format))
            self._config.set("tax_regime", defaults.get("tax_regime", getattr(ruleset, "tax_name", "Tax")))
            self._config.set("jurisdiction_preset", ruleset.id)
            self._config.set("jurisdiction_ruleset_id", ruleset.id)
            self._config.set("jurisdiction_ruleset_version", ruleset.ruleset_version)
            self._config.set("jurisdiction_ruleset_updated_at", ruleset.updated_at)
            currency = str(ruleset.currency or currency).upper()
        elif ruleset is not None:
            self._config.set("jurisdiction_preset", ruleset.id)
            self._config.set("jurisdiction_ruleset_id", ruleset.id)
            self._config.set("jurisdiction_ruleset_version", ruleset.ruleset_version)
            self._config.set("jurisdiction_ruleset_updated_at", ruleset.updated_at)
        self._config.set("allow_custom_rulesets", self._jur_custom.get_active())
        self._config.set("show_guardrails", self._jur_guardrails.get_active())
        self._config.set("verify_tax_rate", self._jur_verify.get_active())

        self._config.set("due_soon_days", due_soon)
        self._config.set("backup_retention_days", backup_days)
        self._config.set("backup_interval_hours", backup_hours)
        self._config.set("auto_backup", self._entries["auto_backup"].get_active_text() == "Yes")
        self._config.set("data_retention_days", data_days)
        self._config.set("prune_old_data", self._entries["prune_old_data"].get_active_text() == "Yes")
        self._config.set("session_timeout_minutes", session_min)
        self._config.set("audit_retention_days", audit_days)
        self._config.set("require_pin", require_pin)
        self._config.set("currency", currency)
        self._config.set("company_name", self._entries["company_name"].get_text().strip())
        self._config.set("fiscal_year_start", self._entries["fiscal_year_start"].get_text().strip() or "01-01")
        self._config.set("gst_rate", gst)
        self._config.set("tax_rate", gst)
        try:
            self._config.save()
        except IOError as e:
            show_error(self, "Could not save preferences: %s" % e)
            return False
        set_format_state(currency, self._config.get("date_format", DEFAULT_DATE_FORMAT))
        # Mirror the settings the database layer reads directly.
        try:
            self._db.set_setting("gst_rate", str(gst))
            self._db.set_setting("tax_rate", str(gst))
            self._db.set_setting("currency", currency)
            self._db.set_setting("company_name", self._config.get("company_name", ""))
            self._db.set_setting("fiscal_year_start", self._config.get("fiscal_year_start", "01-01"))
            self._db.set_setting("data_retention_days", str(data_days))
            self._db.set_setting("audit_retention_days", str(audit_days))
            self._db.set_setting("session_timeout_minutes", str(session_min))
            self._db.set_setting("prune_old_data", "true" if self._config.get("prune_old_data", False) else "false")
            self._db.set_setting("jurisdiction", self._config.get("jurisdiction_preset", DEFAULT_JURISDICTION))
            self._db.set_setting("country", self._config.get("country", DEFAULT_COUNTRY_CODE))
            self._db.set_setting("locale", self._config.get("locale", DEFAULT_LOCALE))
            self._db.set_setting("timezone", self._config.get("timezone", DEFAULT_TIMEZONE))
            self._db.set_setting("date_format", self._config.get("date_format", DEFAULT_DATE_FORMAT))
            self._db.set_setting("tax_regime", self._config.get("tax_regime", DEFAULT_TAX_REGIME))
            self._db.set_setting("jurisdiction_ruleset_version",
                                 self._config.get("jurisdiction_ruleset_version", "1.0.0"))
            self._db.set_setting("jurisdiction_ruleset_updated_at",
                                 self._config.get("jurisdiction_ruleset_updated_at", ""))
            self._db.set_setting("show_guardrails",
                                 "true" if self._config.get("show_guardrails", True) else "false")
            self._db.set_setting("verify_tax_rate",
                                 "true" if self._config.get("verify_tax_rate", True) else "false")
            self._db.set_setting("allow_custom_rulesets",
                                 "true" if self._config.get("allow_custom_rulesets", True) else "false")
        except DatabaseError as e:
            logger.warning("Some settings could not be stored in the database: %s", e)
        return True


def show_about_dialog(parent):
    d = Gtk.AboutDialog(transient_for=parent, modal=True)
    d.set_program_name(APP_NAME)
    d.set_version("%s (build %s)" % (APP_VERSION, APP_BUILD))
    d.set_comments("Marine Vessel Management System\nManage vessels, maintenance, inventory, trips, crew, expenses and compliance.")
    d.set_copyright(APP_COPYRIGHT)
    try:
        d.set_license_type(Gtk.License.MIT_X11)
    except Exception:
        pass
    d.set_logo_icon_name("anchor-symbolic")
    if APP_WEBSITE:
        try:
            d.set_website(APP_WEBSITE)
            d.set_website_label("Project Website")
        except Exception:
            pass
    authors = [APP_VENDOR]
    try:
        d.set_authors(authors)
    except Exception:
        pass
    d.connect("response", lambda dlg, resp: dlg.destroy())
    d.show()

# =============================================================================
# UI COMPONENTS
# =============================================================================
class SummaryCard(Gtk.EventBox):
    def __init__(self, title, value, subtitle="", color="#667eea"):
        super().__init__()
        self._value_label = None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(16)
        tl = Gtk.Label(label=title)
        tl.get_style_context().add_class("card-title")
        tl.set_xalign(0)
        box.pack_start(tl, False, False, 0)
        self._value_label = Gtk.Label(label=value)
        self._value_label.get_style_context().add_class("card-value")
        self._value_label.set_xalign(0)
        box.pack_start(self._value_label, False, False, 0)
        if subtitle:
            sl = Gtk.Label(label=subtitle)
            sl.get_style_context().add_class("card-subtitle")
            sl.set_xalign(0)
            box.pack_start(sl, False, False, 0)
        self.add(box)
        css_cls = f"card-{title.lower().replace(' ', '-').replace('—', 'dash')}"
        p = Gtk.CssProvider()
        p.load_from_data((".%s {background-color:%s;border-radius:8px;}" % (css_cls, color)).encode())
        self.get_style_context().add_provider(p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.get_style_context().add_class(css_cls)
        self.set_property("margin", 4)
        self.set_size_request(170, -1)

    def update(self, val):
        if self._value_label:
            self._value_label.set_text(val)


class UIStateManager:
    def __init__(self, mqs=1000):
        self._actions = Queue(mqs)
        self._proc = False
        self._lock = threading.RLock()
        self._over = 0
        self._shutdown = False

    def request_shutdown(self):
        self._shutdown = True

    def queue(self, fn, *a, **kw):
        if self._shutdown:
            return
        try:
            self._actions.put((fn, a, kw), timeout=0.1)
        except Full:
            self._over += 1
            if self._over % 100 == 0:
                logger.warning("Queue overflow: %d dropped", self._over)
            return
        with self._lock:
            if not self._proc:
                self._proc = True
                GLib.idle_add(self._process)

    def _process(self):
        if self._shutdown:
            return False
        try:
            while True:
                fn, a, kw = self._actions.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception as e:
                    logger.error("UI action failed: %s", e, exc_info=True)
        except Empty:
            pass
        with self._lock:
            if self._actions.empty() or self._shutdown:
                self._proc = False
                return False
            return True


class RefreshManager:
    def __init__(self, app):
        self.app = app
        self._lock = threading.RLock()
        self._refreshing = False
        self._pending = False
        self._timer_armed = False
        self._last = 0.0
        self._min = 1.0
        self._stopped = False
        self._thread = None

    def stop(self, timeout=0.75):
        """Prevent any further refreshes and wait briefly for the current one.

        The wait is bounded: shutdown must never depend on a slow query. A
        hard-exit watchdog in the application guarantees termination if the
        worker is still inside SQLite when the timeout expires.
        """
        self._stopped = True
        with self._lock:
            thread = self._thread
            self._pending = False
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout)

    def schedule(self):
        if self._stopped or getattr(self.app, "_shutting", False):
            return
        now = time.time()
        with self._lock:
            if self._stopped:
                return
            if self._refreshing:
                self._pending = True
                return
            wait = self._min - (now - self._last)
            if wait > 0.01:
                self._pending = True
                if not self._timer_armed:
                    self._timer_armed = True
                    GLib.timeout_add(int(wait * 1000) + 20, self._on_timer)
                return
            self._refreshing = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _on_timer(self):
        with self._lock:
            self._timer_armed = False
        if not self._stopped:
            self.schedule()
        return False

    def _run(self):
        started = time.time()
        try:
            if not self._stopped:
                self._do()
        except Exception as e:
            # Suppress logging once shutdown has started: writing to stdout
            # from a daemon thread during interpreter finalization can abort.
            if not self._stopped and not _EXITING.is_set():
                logger.error("Refresh failed: %s", e, exc_info=True)
        finally:
            with self._lock:
                self._refreshing = False
                self._last = started
                pending = self._pending
                self._pending = False
            if pending and not self._stopped:
                GLib.timeout_add(100, self.schedule)

    def _do(self):
        app = self.app
        if app._shutting or self._stopped:
            return
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
        budgets = app.db.get_budgets()
        fuel_logs = app.db.get_fuel_logs()
        app.ui.queue(app._update_views, v, inv, tasks, isum, tc, vc, trips, suppliers, stock, expenses,
                     finsum, feff, crew, cc, certs, templates, training, watch, budgets, fuel_logs)

# =============================================================================
# MAIN APPLICATION
# =============================================================================
EXPORT_LABELS = {
    'vessels': 'Vessels',
    'inventory': 'Inventory Items',
    'tasks': 'Maintenance Tasks',
    'trips': 'Trip Logs',
    'suppliers': 'Suppliers',
    'stock_transactions': 'Stock Transactions',
    'expenses': 'Expenses',
    'budgets': 'Budgets',
    'fuel_logs': 'Fuel Logs',
    'audit_log': 'Audit Log',
    'crew': 'Crew Members',
    'certifications': 'Certifications',
    'training': 'Training Records',
    'watch_schedules': 'Watch Schedules',
    'task_templates': 'Task Templates',
    'task_parts': 'Task Parts',
}


def _fmt_cell(idx, fmt):
    def apply(col, cell, model, it, data=None):
        v = model.get_value(it, idx)
        kind = fmt[0] if isinstance(fmt, tuple) else fmt
        currency_idx = fmt[1] if isinstance(fmt, tuple) and len(fmt) > 1 else None
        if kind == 'money':
            currency = model.get_value(it, currency_idx) if currency_idx is not None else None
            cell.set_property('text', format_money(v, currency))
        elif kind == 'date':
            cell.set_property('text', format_date(v))
        elif kind == 'datetime':
            cell.set_property('text', format_datetime(v))
        elif kind == 'float':
            cell.set_property('text', "%.1f" % (v or 0.0))
        elif kind == 'pct':
            cell.set_property('text', "%.0f%%" % (v or 0.0))
        elif kind == 'int':
            cell.set_property('text', str(v if v is not None else ""))
        else:
            cell.set_property('text', str(v) if v is not None else "")
    return apply


class VesselKeeperApp:
    def __init__(self):
        self.config = ConfigManager()
        self.registry = _load_jurisdiction_registry(
            allow_custom=bool(self.config.get("allow_custom_rulesets", True)))
        self.jurisdiction = self._resolve_jurisdiction()
        set_format_state(self.config.get("currency", DEFAULT_CURRENCY),
                         self.config.get("date_format", DEFAULT_DATE_FORMAT))
        self.db = ThreadSafeDatabase()
        self._shutting = False
        self._finalized = False
        self._status_pushed = False
        self._locked = False
        self._session_id = uuid.uuid4().hex
        self._last_activity = time.time()
        self._session_timer_id = None
        self._bg_thread = None
        self._bg_stop = threading.Event()
        self._pages = {}
        self._edit_map = {}
        self._del_map = {}
        self._stores = {}
        self._searches = {}
        self._filter_idx = {}
        self._trees = {}
        self._caches = {}
        self._extra_filters = {}
        self._empty_labels = {}
        self._cards = {}
        self._fin_labels = {}

        self.ui = UIStateManager()
        self.refresh = RefreshManager(self)

        self._apply_css()
        self.window = Gtk.Window(title="Vessel Keeper")
        self.window.set_default_size(max(900, int(self.config.get("window_width", 1200))),
                                     max(600, int(self.config.get("window_height", 800))))
        self.window.set_icon_name("anchor-symbolic")
        self.window.connect("delete-event", self._on_delete_window)
        self.window.connect("key-press-event", self._mark_activity)
        self.window.connect("button-press-event", self._mark_activity)

        self._setup_accels()
        self._setup_ui()
        self._setup_menu_actions()
        self._restore_pos()
        self.window.show_all()

        if not self._check_pin_at_startup():
            # Fail closed: do not open the main window when the PIN is wrong.
            self._shutdown()
            return
        self._start_session_timeout()
        self._start_bg()
        self.refresh.schedule()

    # ---- jurisdiction ----
    def _resolve_jurisdiction(self):
        """Return the active ruleset, falling back to T&T then international."""
        if self.registry is None:
            return None
        requested = (self.config.get("jurisdiction_ruleset_id")
                     or self.config.get("jurisdiction_preset")
                     or DEFAULT_JURISDICTION)
        return self.registry.require(requested)

    def _reload_jurisdiction(self):
        """Reload rulesets and re-apply display formats after Preferences."""
        allow_custom = bool(self.config.get("allow_custom_rulesets", True))
        new_registry = _load_jurisdiction_registry(allow_custom=allow_custom)
        if new_registry is not None:
            self.registry = new_registry
        self.jurisdiction = self._resolve_jurisdiction()
        set_format_state(self.config.get("currency", DEFAULT_CURRENCY),
                         self.config.get("date_format", DEFAULT_DATE_FORMAT))
        if self.jurisdiction is not None:
            self._status("Jurisdiction: %s (%s)" % (self.jurisdiction.name,
                                                    self.jurisdiction.country_code))
        self.refresh.schedule()

    def _jurisdiction_options(self):
        """Dialog keyword arguments derived from the active ruleset."""
        rs = self.jurisdiction
        if rs is None:
            return {}
        currency = str(rs.currency or _format_state["currency"]).upper()
        presets = rs.get_crew_cert_presets()
        cert_categories = []
        for preset in presets:
            cat = str(preset.get("category") or "").strip()
            if cat and cat not in cert_categories:
                cert_categories.append(cat)
        return {
            "categories": rs.get_expense_categories(),
            "currencies": [currency] + [c for c in ExpenseDialog.CURRENCIES if c != currency],
            "currency": currency,
            "default_tax_rate": float(self.config.get("tax_rate",
                                                      getattr(rs, "tax_rate", 0.0)) or 0.0),
            "tax_regime": getattr(rs, "tax_name", "Tax"),
            "cert_presets": presets,
            "cert_categories": cert_categories,
            "issuing_body": rs.authorities[0]["name"] if rs.authorities else "",
            "ports_of_registry": list(rs.ports_of_registry),
        }

    def _jurisdiction_context(self):
        return {"db": self.db, "config": self.config, "jurisdiction": self.jurisdiction}

    # ---- status ----
    def _status(self, msg):
        if self._shutting:
            return
        try:
            if self._status_pushed:
                self._statusbar.pop(self._status_ctx)
            self._statusbar.push(self._status_ctx, msg)
            self._status_pushed = True
        except Exception:
            pass

    def _mark_activity(self, *args):
        self._last_activity = time.time()
        return False

    # ---- accelerators ----
    def _setup_accels(self):
        accel = Gtk.AccelGroup()
        self.window.add_accel_group(accel)

        def bind(accel_str, cb):
            key, mod = Gtk.accelerator_parse(accel_str)
            if key:
                accel.connect(key, mod, Gtk.AccelFlags.VISIBLE, lambda *a: cb())

        bind("<Control>f", self._focus_search)
        bind("<Control>e", self._edit_current)
        bind("<Control>d", self._on_delete_current)
        bind("<Control>n", self._add_current)
        bind("<Control>r", lambda: self.refresh.schedule())
        bind("<Control>q", self._shutdown)
        bind("F5", lambda: self.refresh.schedule())
        bind("<Control>p", self._show_prefs)
        bind("<Control>s", self._export_dialog)

    def _on_key_press(self, widget, event):
        self._mark_activity()
        return False

    # ---- PIN / sessions ----
    def _check_pin_at_startup(self):
        if not self.config.get("require_pin", False):
            return True
        stored = str(self.config.get("pin_hash") or "")
        if not stored:
            logger.warning("PIN required but no PIN is configured; skipping lock")
            return True
        if not stored.startswith("pbkdf2_sha256$"):
            # Legacy hashes (hash(pin)) were never stable across processes and
            # cannot be verified safely. Fail open once, disable the stale flag
            # and ask the user to set a new PIN rather than locking them out.
            logger.warning("Legacy PIN hash detected; PIN protection disabled until reset")
            self.config.set("require_pin", False)
            try:
                self.config.save()
            except IOError as e:
                logger.warning("Could not persist PIN reset: %s", e)
            self._info("PIN reset required",
                       "The stored PIN was saved in an outdated format that can no longer be "
                       "verified. PIN protection has been turned off — please set a new PIN "
                       "in File → Preferences → Security.")
            return True
        return self._pin_prompt("Unlock Vessel Keeper")

    def _pin_prompt(self, title):
        dlg = Gtk.Dialog(title=title, transient_for=self.window, flags=0, modal=True)
        dlg.set_default_size(320, -1)
        area = dlg.get_content_area()
        area.set_spacing(8)
        area.set_border_width(14)
        area.pack_start(Gtk.Label(label="Enter PIN:"), False, False, 0)
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        entry.set_activates_default(True)
        area.pack_start(entry, False, False, 0)
        dlg.add_button("Quit", Gtk.ResponseType.CANCEL)
        dlg.add_button("Unlock", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        try:
            for remaining in (3, 2, 1):
                resp = dlg.run()
                if resp != Gtk.ResponseType.OK:
                    return False
                if verify_pin(entry.get_text().strip(), self.config.get("pin_hash", "")):
                    self._mark_activity()
                    return True
                if remaining > 1:
                    show_error(dlg, "Incorrect PIN. %d attempts remaining." % (remaining - 1))
                    entry.set_text("")
            return False
        finally:
            dlg.destroy()

    def _start_session_timeout(self):
        if self._session_timer_id:
            try:
                GLib.source_remove(self._session_timer_id)
            except Exception:
                pass
            self._session_timer_id = None
        minutes = self.config.get("session_timeout_minutes", 30)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes > 0:
            self._session_timer_id = GLib.timeout_add_seconds(30, self._session_check)

    def _session_check(self):
        if self._shutting:
            return False
        try:
            minutes = int(self.config.get("session_timeout_minutes", 30) or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes > 0 and (time.time() - self._last_activity) > minutes * 60:
            self._locked = True
            if not self._pin_prompt("Session Locked - Enter PIN"):
                self._shutdown()
                return False
            self._locked = False
            self._last_activity = time.time()
        return True

    def _focus_search(self):
        name = self._current_page_name()
        entry = self._searches.get(name)
        if entry is not None:
            entry.grab_focus()
        else:
            for e in self._searches.values():
                e.grab_focus()
                break

    # ---- current page dispatch ----
    def _current_page_name(self):
        idx = self.notebook.get_current_page()
        for name, i in self._pages.items():
            if i == idx:
                return name
        return None

    def _add_current(self):
        name = self._current_page_name()
        fn = {
            'vessels': self._add_vessel, 'maintenance': self._add_task,
            'inventory': self._add_item, 'trips': self._add_trip,
            'suppliers': self._add_supplier, 'expenses': self._add_expense,
            'budgets': self._add_budget, 'fuel': self._add_fuel,
            'crew': self._add_crew, 'certifications': self._add_cert,
            'training': self._add_training, 'watch': self._add_watch,
        }.get(name)
        if fn:
            fn()

    def _edit_current(self):
        name = self._current_page_name()
        fn = self._edit_map.get(name)
        if fn:
            fn()

    def _on_delete_current(self):
        name = self._current_page_name()
        fn = self._del_map.get(name)
        if fn:
            fn()

    def _selected_id(self, name):
        tv = self._trees.get(name)
        if tv is None:
            return None
        model, it = tv.get_selection().get_selected()
        if it is None:
            return None
        return model.get_value(it, 0)

    def _update_toolbar_state(self):
        name = self._current_page_name()
        editable = {'vessels', 'maintenance', 'inventory', 'trips', 'suppliers',
                    'expenses', 'budgets', 'fuel', 'crew', 'certifications',
                    'training', 'watch'}
        self._btn_add.set_sensitive(name in editable)
        self._btn_edit.set_sensitive(name in editable)
        self._btn_del.set_sensitive(name in editable or name == 'stock')
        self._status("Viewing: %s" % (name or "dashboard").replace("_", " ").title())

    # ---- menus ----
    def _setup_menu_actions(self):
        # Menu handlers are wired in _setup_ui; nothing extra needed here.
        pass

    def _setup_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.add(vbox)

        menubar = Gtk.MenuBar()

        def menu_item(menu, label, cb):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda w: cb())
            menu.append(it)
            return it

        # File
        file_menu = Gtk.Menu()
        file_top = Gtk.MenuItem(label="File")
        file_top.set_submenu(file_menu)
        menubar.append(file_top)
        menu_item(file_menu, "Export Data…", self._export_dialog)
        menu_item(file_menu, "Backup Now", self._manual_backup)
        try:
            menu_item(file_menu, "Restore from Backup…", self._restore_from_backup_dialog)
        except Exception:
            pass
        try:
            menu_item(file_menu, "Export Diagnostics Bundle…", self._export_diagnostics_ui)
        except Exception:
            pass
        file_menu.append(Gtk.SeparatorMenuItem())
        menu_item(file_menu, "Preferences…", self._show_prefs)
        file_menu.append(Gtk.SeparatorMenuItem())
        menu_item(file_menu, "Quit", self._shutdown)

        # Edit
        edit_menu = Gtk.Menu()
        edit_top = Gtk.MenuItem(label="Edit")
        edit_top.set_submenu(edit_menu)
        menubar.append(edit_top)
        menu_item(edit_menu, "Add New", self._add_current)
        menu_item(edit_menu, "Edit Selected", self._edit_current)
        menu_item(edit_menu, "Delete Selected", self._on_delete_current)
        edit_menu.append(Gtk.SeparatorMenuItem())
        menu_item(edit_menu, "Find", self._focus_search)
        menu_item(edit_menu, "Refresh", lambda: self.refresh.schedule())

        # Tools
        tools_menu = Gtk.Menu()
        tools_top = Gtk.MenuItem(label="Tools")
        tools_top.set_submenu(tools_menu)
        menubar.append(tools_top)
        menu_item(tools_menu, "Task Templates…", self._manage_templates)
        menu_item(tools_menu, "Audit Log…", self._show_audit_log)
        tools_menu.append(Gtk.SeparatorMenuItem())
        menu_item(tools_menu, "Export All Personal Data (JSON)…", self._gdpr_export)
        menu_item(tools_menu, "Delete All Data…", self._gdpr_delete)

        # Help
        help_menu = Gtk.Menu()
        help_top = Gtk.MenuItem(label="Help")
        help_top.set_submenu(help_menu)
        menubar.append(help_top)
        menu_item(help_menu, "About", lambda: show_about_dialog(self.window))

        vbox.pack_start(menubar, False, False, 0)

        vbox.pack_start(self._make_toolbar(), False, False, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.connect("switch-page", self._on_switch_page)
        vbox.pack_start(self.notebook, True, True, 0)

        pages = [
            ('dashboard', 'Dashboard', self._dash_tab()),
            ('vessels', 'Vessels', self._vessel_tab()),
            ('maintenance', 'Maintenance', self._mtab()),
            ('inventory', 'Inventory', self._inventory_tab()),
            ('trips', 'Trips', self._trip_tab()),
            ('suppliers', 'Suppliers', self._supplier_tab()),
            ('stock', 'Stock History', self._stock_tab()),
            ('expenses', 'Expenses', self._expense_tab()),
            ('budgets', 'Budgets', self._budget_tab()),
            ('fuel', 'Fuel Logs', self._fuel_tab()),
            ('finance', 'Finance', self._finance_tab()),
            ('compliance', 'Compliance', self._compliance_tab()),
            ('crew', 'Crew', self._crew_tab()),
            ('certifications', 'Certifications', self._cert_tab()),
            ('training', 'Training', self._training_tab()),
            ('watch', 'Watch Schedule', self._watch_tab()),
        ]
        for i, (name, label, widget) in enumerate(pages):
            self._pages[name] = i
            self.notebook.append_page(widget, Gtk.Label(label=label))

        self._edit_map = {
            'vessels': self._edit_vessel, 'maintenance': self._edit_task,
            'inventory': self._edit_item, 'trips': self._edit_trip,
            'suppliers': self._edit_supplier, 'expenses': self._edit_expense,
            'budgets': self._edit_budget, 'fuel': self._edit_fuel,
            'crew': self._edit_crew, 'certifications': self._edit_cert,
            'training': self._edit_training, 'watch': self._edit_watch,
        }
        self._del_map = {
            'vessels': self._del_vessel, 'maintenance': self._del_task,
            'inventory': self._del_item, 'trips': self._del_trip,
            'suppliers': self._del_supplier, 'stock': self._del_stock,
            'expenses': self._del_expense, 'budgets': self._del_budget,
            'fuel': self._del_fuel, 'crew': self._del_crew,
            'certifications': self._del_cert, 'training': self._del_training,
            'watch': self._del_watch,
        }

        self._statusbar = Gtk.Statusbar()
        self._status_ctx = self._statusbar.get_context_id("main")
        vbox.pack_start(self._statusbar, False, False, 0)
        self._update_toolbar_state()

    def _make_toolbar(self):
        tb = Gtk.Toolbar()
        tb.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        self._btn_add = Gtk.ToolButton(label="Add", icon_name="list-add-symbolic")
        self._btn_add.connect("clicked", lambda w: self._add_current())
        tb.insert(self._btn_add, -1)
        self._btn_edit = Gtk.ToolButton(label="Edit", icon_name="document-edit-symbolic")
        self._btn_edit.connect("clicked", lambda w: self._edit_current())
        tb.insert(self._btn_edit, -1)
        self._btn_del = Gtk.ToolButton(label="Delete", icon_name="edit-delete-symbolic")
        self._btn_del.connect("clicked", lambda w: self._on_delete_current())
        tb.insert(self._btn_del, -1)
        tb.insert(Gtk.SeparatorToolItem(), -1)
        btn_refresh = Gtk.ToolButton(label="Refresh", icon_name="view-refresh-symbolic")
        btn_refresh.connect("clicked", lambda w: self.refresh.schedule())
        tb.insert(btn_refresh, -1)
        btn_export = Gtk.ToolButton(label="Export", icon_name="document-save-symbolic")
        btn_export.connect("clicked", lambda w: self._export_dialog())
        tb.insert(btn_export, -1)
        tb.insert(Gtk.SeparatorToolItem(), -1)
        btn_prefs = Gtk.ToolButton(label="Preferences", icon_name="preferences-system-symbolic")
        btn_prefs.connect("clicked", lambda w: self._show_prefs())
        tb.insert(btn_prefs, -1)
        return tb

    def _on_switch_page(self, notebook, child, idx):
        self._update_toolbar_state()
        name = None
        for n, i in self._pages.items():
            if i == idx:
                name = n
                break
        if name == 'compliance':
            self._refresh_compliance()

    # ---- generic table page ----
    def _table_page(self, name, types, cols, placeholder="Search…", filter_indices=(),
                    extras=None, on_activate=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.SearchEntry()
        entry.set_placeholder_text(placeholder)
        entry.connect("search-changed", lambda e: self._refill(name))
        top.pack_start(entry, False, False, 0)
        for w in (extras or ()):
            top.pack_start(w, False, False, 0)
        box.pack_start(top, False, False, 0)

        store = Gtk.ListStore(*types)
        tv = self._treeview(cols, store)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)

        empty = Gtk.Label(label="No records yet — use Add to create one.")
        empty.get_style_context().add_class("dim-label")
        empty.set_xalign(0.5)
        box.pack_start(empty, False, False, 4)

        self._stores[name] = store
        self._searches[name] = entry
        self._filter_idx[name] = tuple(filter_indices)
        self._trees[name] = tv
        self._empty_labels[name] = empty
        self._caches[name] = []
        tv.connect("button-press-event", self._right_click)
        tv.connect("row-activated", on_activate or (lambda *a: self._edit_current()))
        return box

    def _treeview(self, cols, store):
        tv = Gtk.TreeView(model=store)
        tv.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)
        tv.set_enable_search(False)
        for spec in cols:
            title, idx, width, fmt = spec[:4]
            visible = spec[4] if len(spec) > 4 else True
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", 3)
            col = Gtk.TreeViewColumn(title, r)
            col.set_sort_column_id(idx)
            col.set_resizable(True)
            if width:
                col.set_min_width(width)
            fmt_kind = fmt[0] if isinstance(fmt, tuple) else fmt
            if fmt_kind in ('money', 'float', 'pct', 'int'):
                col.set_cell_data_func(r, _fmt_cell(idx, fmt))
            else:
                col.add_attribute(r, 'text', idx)
            col.set_visible(visible)
            tv.append_column(col)
        return tv

    def _set_rows(self, name, rows):
        self._caches[name] = rows
        self._refill(name)

    def _refill(self, name):
        store = self._stores.get(name)
        if store is None:
            return
        text = ""
        entry = self._searches.get(name)
        if entry is not None:
            text = entry.get_text().strip().lower()
        idxs = self._filter_idx.get(name, ())
        extra = self._extra_filters.get(name)
        store.clear()
        for row in self._caches.get(name, []):
            if extra is not None and not extra(row):
                continue
            if text and idxs:
                haystack = " ".join(str(row[i]) for i in idxs if i < len(row)).lower()
                if text not in haystack:
                    continue
            store.append(row)
        lbl = self._empty_labels.get(name)
        if lbl is not None:
            if len(store) == 0:
                lbl.set_text("No matching records." if text else "No records yet — use Add to create one.")
                lbl.set_visible(True)
            else:
                lbl.set_visible(False)

    def _right_click(self, tv, event):
        if event.button != 3:
            return False
        path = tv.get_path_at_pos(int(event.x), int(event.y))
        if path:
            tv.get_selection().select_path(path[0])
        name = self._current_page_name()
        menu = Gtk.Menu()
        can_modify = name in self._edit_map
        can_delete = name in self._del_map
        if can_modify:
            add_it = Gtk.MenuItem(label="Add")
            add_it.connect("activate", lambda w: self._add_current())
            menu.append(add_it)
            edit_it = Gtk.MenuItem(label="Edit")
            edit_it.connect("activate", lambda w: self._edit_current())
            menu.append(edit_it)
        if can_delete:
            del_it = Gtk.MenuItem(label="Delete")
            del_it.connect("activate", lambda w: self._on_delete_current())
            menu.append(del_it)
        if can_modify or can_delete:
            menu.append(Gtk.SeparatorMenuItem())
        ref_it = Gtk.MenuItem(label="Refresh")
        ref_it.connect("activate", lambda w: self.refresh.schedule())
        menu.append(ref_it)
        menu.attach_to_widget(tv, None)
        menu.connect("selection-done", lambda m: m.destroy())
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # ---- export / compliance ----
    def _export_dialog(self):
        dlg = Gtk.Dialog(title="Export Data", transient_for=self.window, flags=0, modal=True)
        dlg.set_default_size(420, -1)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Export…", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)
        area.pack_start(Gtk.Label(label="Choose a dataset to export as CSV:"), False, False, 0)
        combo = Gtk.ComboBoxText()
        keys = sorted(EXPORT_QUERIES.keys())
        for k in keys:
            combo.append(k, EXPORT_LABELS.get(k, k))
        combo.set_active(0)
        area.pack_start(combo, False, False, 0)
        dlg.show_all()
        key = None
        if dlg.run() == Gtk.ResponseType.OK:
            key = combo.get_active_id()
        dlg.destroy()
        if not key:
            return
        chooser = Gtk.FileChooserDialog(title="Save CSV", transient_for=self.window,
                                        action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name("%s_%s.csv" % (key, datetime.date.today().isoformat()))
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                path = chooser.get_filename()
                if not path:
                    return
                try:
                    count = self.db.export_csv(path, EXPORT_QUERIES[key])
                except (ValidationError, DatabaseError, OSError) as e:
                    show_error(self.window, str(e))
                    return
                if count:
                    self._info("Export complete", "%d rows written to\n%s" % (count, path))
                else:
                    self._info("Nothing to export", "The %s dataset is empty." % EXPORT_LABELS.get(key, key))
        finally:
            chooser.destroy()

    def _info(self, title, msg):
        d = Gtk.MessageDialog(transient_for=self.window, flags=0, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(msg)
        d.run()
        d.destroy()

    def _show_prefs(self):
        dlg = PreferencesDialog(self.window, self.config, self.db, registry=self.registry)
        try:
            while True:
                resp = dlg.run()
                if resp != Gtk.ResponseType.OK:
                    break
                if dlg.apply():
                    self._status("Preferences saved")
                    self._reload_jurisdiction()
                    self._start_session_timeout()
                    break
        finally:
            dlg.destroy()

    def _gdpr_export(self):
        chooser = Gtk.FileChooserDialog(title="Save Personal Data Export", transient_for=self.window,
                                        action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name("vessel_keeper_data_%s.json" % datetime.date.today().isoformat())
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                path = chooser.get_filename()
                if not path:
                    return
                try:
                    data = self.db.export_all_user_data()
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2, default=str)
                except (DatabaseError, OSError) as e:
                    show_error(self.window, str(e))
                    return
                self._info("Export complete", "Personal data written to\n%s" % path)
        finally:
            chooser.destroy()

    def _gdpr_delete(self):
        d = Gtk.MessageDialog(transient_for=self.window, flags=0, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text="Delete all data?")
        d.format_secondary_text("This permanently removes every vessel, task, trip, crew member, "
                                "expense and audit entry. This cannot be undone.")
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        d.add_button("Delete Everything", Gtk.ResponseType.OK)
        resp = d.run()
        d.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        try:
            self.db.delete_all_user_data()
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        self.refresh.schedule()
        self._status("All user data deleted")

    def _show_audit_log(self):
        dlg = Gtk.Dialog(title="Audit Log", transient_for=self.window, flags=0, modal=True)
        dlg.set_default_size(900, 520)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        area = dlg.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        store = Gtk.ListStore(str, str, str, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, n in [(0, "Timestamp"), (1, "Action"), (2, "Entity"), (3, "ID"), (4, "User"), (5, "Session")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        area.pack_start(sw, True, True, 0)
        try:
            rows = self.db.get_audit_log(1000)
        except DatabaseError as e:
            show_error(dlg, str(e))
            rows = []
        for r in rows:
            store.append([format_datetime(r.get('timestamp', '')), r.get('action', ''),
                          r.get('entity_type', ''), str(r.get('entity_id') or ''),
                          r.get('user', ''), r.get('session_id') or ''])
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _manage_templates(self):
        dlg = Gtk.Dialog(title="Task Templates", transient_for=self.window, flags=0, modal=True)
        dlg.set_default_size(720, 460)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        area = dlg.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        tb = Gtk.Toolbar()
        btn_add = Gtk.ToolButton(label="Add", icon_name="list-add-symbolic")
        btn_add.connect("clicked", lambda w: self._template_add(store))
        tb.insert(btn_add, -1)
        btn_edit = Gtk.ToolButton(label="Edit", icon_name="document-edit-symbolic")
        btn_edit.connect("clicked", lambda w: self._template_edit(store, tv))
        tb.insert(btn_edit, -1)
        btn_del = Gtk.ToolButton(label="Delete", icon_name="edit-delete-symbolic")
        btn_del.connect("clicked", lambda w: self._template_delete(store, tv))
        tb.insert(btn_del, -1)
        area.pack_start(tb, False, False, 0)
        store = Gtk.ListStore(int, str, str, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, n in [(1, "Name"), (2, "System"), (3, "Priority"), (4, "Interval (days)"), (5, "Interval (hours)")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(tv)
        area.pack_start(sw, True, True, 0)
        self._load_templates_into(store)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _load_templates_into(self, store):
        store.clear()
        try:
            rows = self.db.get_task_templates()
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        for t in rows:
            store.append([t['id'], t['name'], t.get('system') or '', t.get('priority') or '',
                          str(t.get('interval_days') or ''), str(t.get('interval_hours') or '')])

    def _template_add(self, store):
        d = TemplateDialog(self.window, self.db)
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.add_task_template(d.get_template())
                self._load_templates_into(store)
                self.refresh.schedule()
            except (ValidationError, DatabaseError) as e:
                show_error(self.window, str(e))
        d.destroy()

    def _template_edit(self, store, tv):
        model, it = tv.get_selection().get_selected()
        if it is None:
            return
        tid = model.get_value(it, 0)
        try:
            row = next((r for r in self.db.get_task_templates() if r['id'] == tid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = TemplateDialog(self.window, self.db, TaskTemplate.from_dict(row))
        if d.run() == Gtk.ResponseType.OK:
            try:
                self.db.update_task_template(d.get_template())
                self._load_templates_into(store)
                self.refresh.schedule()
            except (ValidationError, DatabaseError) as e:
                show_error(self.window, str(e))
        d.destroy()

    def _template_delete(self, store, tv):
        model, it = tv.get_selection().get_selected()
        if it is None:
            return
        tid = model.get_value(it, 0)
        if confirm(self.window, "Delete this template?"):
            try:
                self.db.delete_task_template(tid)
                self._load_templates_into(store)
                self.refresh.schedule()
            except DatabaseError as e:
                show_error(self.window, str(e))

    # ---- dashboard ----
    def _card(self, key, title, value, color, subtitle=""):
        card = SummaryCard(title, value, subtitle, color)
        self._cards[key] = card
        return card

    def _dash_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(14)
        heading = Gtk.Label()
        heading.set_markup("<b><big>Fleet Overview</big></b>")
        heading.set_xalign(0)
        outer.pack_start(heading, False, False, 0)
        sub = Gtk.Label()
        sub.set_xalign(0)
        sub.set_line_wrap(True)
        if self.jurisdiction is not None:
            rs = self.jurisdiction
            sub.set_markup(
                "Jurisdiction: <b>%s</b> · %s %.2f%% · %s · <i>advisory presets, verify with %s</i>"
                % (GLib.markup_escape_text(rs.name),
                   GLib.markup_escape_text(str(getattr(rs, "tax_name", "Tax"))),
                   float(self.config.get("tax_rate", getattr(rs, "tax_rate", 0.0)) or 0.0),
                   GLib.markup_escape_text(rs.currency),
                   GLib.markup_escape_text(rs.authorities[0]["name"]
                                           if rs.authorities else "the relevant authority")))
        else:
            sub.set_text("Jurisdiction rulesets are unavailable; default advisory checks are off.")
        sub.get_style_context().add_class("dim-label")
        outer.pack_start(sub, False, False, 0)

        flow = Gtk.FlowBox()
        flow.set_max_children_per_line(4)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_row_spacing(6)
        flow.set_column_spacing(6)
        cards = [
            ("vessels", "Vessels", "0", "#667eea", "in the fleet"),
            ("tasks", "Open Tasks", "0", "#4facfe", "pending work"),
            ("overdue", "Overdue", "0", "#f5576c", "need attention"),
            ("due_soon", "Due Soon", "0", "#f6a623", "within threshold"),
            ("low_stock", "Low Stock", "0", "#e67e22", "items at minimum"),
            ("value", "Inventory Value", format_money(0), "#00b894", "on hand"),
            ("crew", "Active Crew", "0", "#9b59b6", "on the books"),
            ("cert_expiry", "Cert Alerts", "0", "#e74c3c", "expiring soon"),
        ]
        for key, title, val, color, sub in cards:
            flow.add(self._card(key, title, val, color, sub))
        outer.pack_start(flow, False, False, 0)

        tips = Gtk.Label()
        tips.set_markup("<b>Quick tips</b>\n"
                        "• Add vessels first, then maintenance tasks, trips and crew.\n"
                        "• Adjust stock from Inventory; every change is recorded in Stock History.\n"
                        "• Press Ctrl+F to search the current list, F5 to refresh, Ctrl+E to edit.\n"
                        "• Use Tools → Audit Log to see every change, and File → Export Data for CSV reports.")
        tips.set_xalign(0)
        tips.set_line_wrap(True)
        tips.get_style_context().add_class("dim-label")
        outer.pack_start(tips, False, False, 6)

        recent = Gtk.Label()
        recent.set_markup("<b>Recent Maintenance</b>")
        recent.set_xalign(0)
        outer.pack_start(recent, False, False, 0)
        self._dash_store = Gtk.ListStore(str, str, str, str)
        self._dash_tv = Gtk.TreeView(model=self._dash_store)
        for i, n in [(0, "Vessel"), (1, "Task"), (2, "Due"), (3, "Status")]:
            r = Gtk.CellRendererText()
            c = Gtk.TreeViewColumn(n, r)
            if i == 2:
                c.set_cell_data_func(r, _fmt_cell(i, 'date'))
            else:
                c.add_attribute(r, 'text', i)
            c.set_resizable(True)
            self._dash_tv.append_column(c)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(self._dash_tv)
        outer.pack_start(sw, True, True, 0)
        return outer

    # =========================================================================
    # TABS
    # =========================================================================
    def _vessel_tab(self):
        return self._table_page(
            'vessels', [int, str, str, str, str, float, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("Name", 1, 180, 'str'), ("Type", 2, 120, 'str'),
             ("Engine", 3, 180, 'str'), ("Location", 4, 160, 'str'),
             ("Engine Hours", 5, 110, 'float'), ("Flag State", 6, 150, 'str'),
             ("Port of Registry", 7, 150, 'str'), ("Official No.", 8, 110, 'str'),
             ("IMO No.", 9, 100, 'str'), ("Reg. Expiry", 10, 110, 'date')],
            "Search vessels…", (1, 2, 3, 4, 6, 7, 8, 9))

    def _filter_vessels(self, row):
        return True

    def _inventory_tab(self):
        adj = Gtk.Button(label="Adjust Stock…")
        adj.connect("clicked", lambda w: self._adjust_stock())
        return self._table_page(
            'inventory', [int, str, str, str, int, int, float, str],
            [("ID", 0, 50, 'int'), ("Item", 1, 180, 'str'), ("Part #", 2, 120, 'str'),
             ("Category", 3, 120, 'str'), ("Stock", 4, 70, 'int'), ("Min", 5, 60, 'int'),
             ("Unit Cost", 6, 90, 'money'), ("Supplier", 7, 150, 'str')],
            "Search inventory…", (1, 2, 3, 7), extras=[adj])

    def _filter_items(self, row):
        return True

    def _adjust_stock(self):
        iid = self._selected_id('inventory')
        if iid is None:
            self._status("Select an inventory item first")
            return
        try:
            item = self.db.get_item(iid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not item:
            return
        dlg = StockAdjustDialog(self.window, item['name'], item['current_stock'])
        try:
            if dlg.run() == Gtk.ResponseType.OK:
                try:
                    delta = dlg.get_delta()
                    if delta == 0:
                        return
                    self.db.adjust_stock(iid, delta, dlg.get_note())
                    self.refresh.schedule()
                    self._status("Stock adjusted for %s" % item['name'])
                except (ValidationError, DatabaseError) as e:
                    show_error(self.window, str(e))
        finally:
            dlg.destroy()

    def _mtab(self):
        self._task_status = "All"
        status_combo = Gtk.ComboBoxText()
        for s in ("All", "pending", "in_progress", "overdue", "completed"):
            status_combo.append(s, s.replace("_", " ").title())
        status_combo.set_active(0)

        def on_status(combo):
            self._task_status = combo.get_active_id() or "All"
            self._refill('maintenance')

        status_combo.connect("changed", on_status)

        complete_btn = Gtk.Button(label="Complete…")
        complete_btn.connect("clicked", lambda w: self._complete_task())
        parts_btn = Gtk.Button(label="Parts…")
        parts_btn.connect("clicked", lambda w: self._manage_task_parts())
        from_tmpl_btn = Gtk.Button(label="New from Template…")
        from_tmpl_btn.connect("clicked", lambda w: self._from_template())
        tmpl_btn = Gtk.Button(label="Templates…")
        tmpl_btn.connect("clicked", lambda w: self._manage_templates())

        page = self._table_page(
            'maintenance', [int, str, str, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("Vessel", 1, 140, 'str'), ("Task", 2, 220, 'str'),
             ("System", 3, 120, 'str'), ("Due", 4, 100, 'date'), ("Status", 5, 100, 'str'),
             ("Priority", 6, 85, 'str'), ("Work Order", 7, 100, 'str')],
            "Search tasks…", (1, 2, 3, 4, 5, 6, 7),
            extras=[status_combo, complete_btn, parts_btn, from_tmpl_btn, tmpl_btn])
        self._extra_filters['maintenance'] = self._filter_tasks
        return page

    def _filter_tasks(self, row):
        status = getattr(self, '_task_status', "All")
        return status == "All" or row[5] == status

    def _complete_task(self):
        tid = self._selected_id('maintenance')
        if tid is None:
            self._status("Select a task first")
            return
        try:
            task = self.db.get_task(tid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not task:
            return
        if task.get('status') == 'completed':
            self._info("Already completed", "This task is already marked as completed.")
            return
        dlg = CompleteTaskDialog(self.window, tid, task.get('task_name', ''))
        try:
            if dlg.run() == Gtk.ResponseType.OK:
                try:
                    by, notes, cost = dlg.get_data()
                    self.db.complete_task(tid, by, notes, cost)
                    self.refresh.schedule()
                    self._status("Task completed: %s" % task.get('task_name', ''))
                except (ValidationError, DatabaseError) as e:
                    show_error(self.window, str(e))
        finally:
            dlg.destroy()

    def _manage_task_parts(self):
        tid = self._selected_id('maintenance')
        if tid is None:
            self._status("Select a task first")
            return
        name = ""
        for row in self._caches.get('maintenance', []):
            if row[0] == tid:
                name = row[2]
                break
        dlg = TaskPartsDialog(self.window, self.db, tid, name)
        try:
            dlg.run()
        finally:
            dlg.destroy()
        self.refresh.schedule()

    def _from_template(self):
        try:
            templates = self.db.get_task_templates()
            vessels = self.db.get_vessels()
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not templates:
            show_error(self.window, "No task templates yet. Create one with Templates…")
            return
        if not vessels:
            show_error(self.window, "Add a vessel before creating maintenance tasks.")
            return
        dlg = Gtk.Dialog(title="New Task from Template", transient_for=self.window, flags=0, modal=True)
        dlg.set_default_size(420, -1)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Create", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)
        area.pack_start(Gtk.Label(label="Choose a task template:"), False, False, 0)
        combo = Gtk.ComboBoxText()
        for t in templates:
            label = "%s — %s" % (t.get('name') or '', t.get('system') or 'General')
            combo.append(str(t['id']), label)
        combo.set_active(0)
        area.pack_start(combo, False, False, 0)
        note = Gtk.Label(label="You can review and adjust the task before saving.")
        note.get_style_context().add_class("dim-label")
        note.set_xalign(0)
        area.pack_start(note, False, False, 0)
        dlg.show_all()
        try:
            if dlg.run() != Gtk.ResponseType.OK:
                return
            selected = int(combo.get_active_id())
        finally:
            dlg.destroy()
        template = next((t for t in templates if t['id'] == selected), None)
        if not template:
            return
        vessel = vessels[0]
        if template.get('default_vessel_type'):
            vessel = next((v for v in vessels
                           if (v.get('vessel_type') or '') == template['default_vessel_type']), vessels[0])
        days = template.get('interval_days')
        due = datetime.date.today() + datetime.timedelta(days=int(days or 0))
        task = MaintenanceTask(
            vessel_id=vessel['id'], vessel_name=vessel['name'],
            system=template.get('system') or '', task_name=template.get('name') or '',
            description=template.get('description') or '', next_due=due.isoformat(),
            status='pending', priority=template.get('priority') or 'medium',
            interval_days=days, interval_hours=template.get('interval_hours'),
            is_recurring=bool(days or template.get('interval_hours')))
        d = MaintenanceDialog(self.window, self.db, task)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_task, self.db.add_task, "Task created from template")
        finally:
            d.destroy()

    def _trip_tab(self):
        return self._table_page(
            'trips', [int, str, str, str, str, float, float],
            [("ID", 0, 50, 'int'), ("Vessel", 1, 140, 'str'), ("Departure", 2, 120, 'str'),
             ("Arrival", 3, 120, 'str'), ("Destination", 4, 160, 'str'),
             ("Distance (nm)", 5, 110, 'float'), ("Fuel Used", 6, 100, 'float')],
            "Search trips…", (1, 2, 3, 4))

    def _supplier_tab(self):
        return self._table_page(
            'suppliers', [int, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("Supplier", 1, 190, 'str'), ("Contact", 2, 150, 'str'),
             ("Phone", 3, 130, 'str'), ("Email", 4, 200, 'str'),
             ("Preferred", 5, 90, 'str')],
            "Search suppliers…", (1, 2, 3, 4))

    def _stock_tab(self):
        return self._table_page(
            'stock', [int, str, str, int, str, str],
            [("ID", 0, 50, 'int'), ("Item", 1, 190, 'str'), ("Type", 2, 100, 'str'),
             ("Quantity", 3, 90, 'int'), ("Notes", 4, 250, 'str'), ("When", 5, 160, 'datetime')],
            "Search stock history…", (1, 2, 4, 5))

    def _expense_tab(self):
        return self._table_page(
            'expenses', [int, str, str, str, float, float, float, str, str, str],
            [("ID", 0, 50, 'int'), ("Date", 1, 100, 'date'), ("Vessel", 2, 130, 'str'),
             ("Category", 3, 110, 'str'), ("Amount", 4, 100, ('money', 7)),
             ("Tax", 5, 90, ('money', 7)), ("Total", 6, 100, ('money', 7)),
             ("Curr", 7, 60, 'str'), ("Vendor", 8, 140, 'str'),
             ("Description", 9, 220, 'str')],
            "Search expenses…", (1, 2, 3, 8, 9))

    def _budget_tab(self):
        return self._table_page(
            'budgets', [int, str, int, str, float, float, float, float],
            [("ID", 0, 50, 'int'), ("Vessel", 1, 140, 'str'), ("Year", 2, 70, 'int'),
             ("Category", 3, 120, 'str'), ("Budget", 4, 100, 'money'),
             ("Spent", 5, 100, 'money'), ("Remaining", 6, 100, 'money'),
             ("Used", 7, 90, 'pct')],
            "Search budgets…", (1, 3))

    def _fuel_tab(self):
        return self._table_page(
            'fuel', [int, str, str, float, float, float, str, str],
            [("ID", 0, 50, 'int'), ("Vessel", 1, 140, 'str'), ("Date", 2, 150, 'datetime'),
             ("Gallons", 3, 90, 'float'), ("Cost/Gal", 4, 95, 'money'),
             ("Total Cost", 5, 100, 'money'), ("Fuel Type", 6, 110, 'str'),
             ("Vendor / Location", 7, 200, 'str')],
            "Search fuel logs…", (1, 2, 6, 7))

    def _finance_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(12)
        heading = Gtk.Label()
        heading.set_markup("<b><big>Financial Summary</big></b>")
        heading.set_xalign(0)
        outer.pack_start(heading, False, False, 0)
        grid = Gtk.Grid()
        grid.set_column_spacing(18)
        grid.set_row_spacing(6)
        labels = [
            ("year", "Fiscal Year"),
            ("total_expenses", "Total Expenses"),
            ("expense_count", "Expense Entries"),
            ("total_budget", "Total Budget"),
            ("budget_spent", "Budget-Attributed Spend"),
            ("utilization", "Budget Utilisation"),
            ("fuel_cost", "Total Fuel Cost"),
        ]
        for i, (key, label) in enumerate(labels):
            lbl = Gtk.Label(label=label + ":", xalign=0)
            lbl.get_style_context().add_class("dim-label")
            val = Gtk.Label(label="—", xalign=0)
            val.get_style_context().add_class("summary-value")
            grid.attach(lbl, 0, i, 1, 1)
            grid.attach(val, 1, i, 1, 1)
            self._fin_labels[key] = val
        outer.pack_start(grid, False, False, 0)

        cols_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.pack_start(cols_box, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ll = Gtk.Label(label="Spending by Category", xalign=0)
        ll.set_markup("<b>Spending by Category</b>")
        left.pack_start(ll, False, False, 0)
        self._fin_store = Gtk.ListStore(str, float, float)
        fin_tv = self._treeview(
            [("Category", 0, 130, 'str'), ("Amount", 1, 110, 'money'), ("Share", 2, 80, 'pct')],
            self._fin_store)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(fin_tv)
        left.pack_start(sw, True, True, 0)
        cols_box.pack_start(left, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rl = Gtk.Label(xalign=0)
        rl.set_markup("<b>Fuel Efficiency by Vessel</b>")
        right.pack_start(rl, False, False, 0)
        self._feff_store = Gtk.ListStore(str, float, float, float, float)
        feff_tv = self._treeview(
            [("Vessel", 0, 120, 'str'), ("Distance (nm)", 1, 110, 'float'),
             ("Gallons", 2, 90, 'float'), ("Fuel Cost", 3, 100, 'money'),
             ("NMPG", 4, 80, 'float')],
            self._feff_store)
        sw2 = Gtk.ScrolledWindow()
        sw2.set_vexpand(True)
        sw2.add(feff_tv)
        right.pack_start(sw2, True, True, 0)
        cols_box.pack_start(right, True, True, 0)
        return outer

    def _crew_tab(self):
        cert_btn = Gtk.Button(label="Certifications…")
        cert_btn.connect("clicked", lambda w: self._manage_crew_certs())
        return self._table_page(
            'crew', [int, str, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("First Name", 1, 130, 'str'), ("Last Name", 2, 130, 'str'),
             ("Role", 3, 130, 'str'), ("Status", 4, 90, 'str'), ("Email", 5, 200, 'str'),
             ("Phone", 6, 130, 'str')],
            "Search crew…", (1, 2, 3, 4, 5, 6), extras=[cert_btn])

    def _cert_tab(self):
        return self._table_page(
            'certifications', [int, str, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("Crew Member", 1, 150, 'str'),
             ("Certification", 2, 200, 'str'), ("Issuing Body", 3, 150, 'str'),
             ("Number", 4, 110, 'str'), ("Issued", 5, 100, 'date'), ("Expires", 6, 100, 'date')],
            "Search certifications…", (1, 2, 3, 4))

    def _training_tab(self):
        return self._table_page(
            'training', [int, str, str, str, str, str, float],
            [("ID", 0, 50, 'int'), ("Crew Member", 1, 150, 'str'), ("Course", 2, 210, 'str'),
             ("Provider", 3, 150, 'str'), ("Completed", 4, 110, 'date'),
             ("Expires", 5, 110, 'date'), ("Cost", 6, 90, 'money')],
            "Search training…", (1, 2, 3, 4, 5))

    def _watch_tab(self):
        return self._table_page(
            'watch', [int, str, str, str, str, str, str],
            [("ID", 0, 50, 'int'), ("Date", 1, 100, 'date'), ("Crew Member", 2, 150, 'str'),
             ("Vessel", 3, 140, 'str'), ("Start", 4, 80, 'str'), ("End", 5, 80, 'str'),
             ("Role", 6, 150, 'str')],
            "Search watch schedule…", (1, 2, 3, 4, 5, 6))

    # ---- compliance tab ----
    def _compliance_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(10)

        self._compliance_header = Gtk.Label()
        self._compliance_header.set_xalign(0)
        self._compliance_header.set_line_wrap(True)
        outer.pack_start(self._compliance_header, False, False, 0)

        disclaimer = Gtk.Label()
        disclaimer.set_markup(
            "<i>Guard rails only — this is not legal advice and is not a statutory filing. "
            "Tax rates, certificate requirements and reporting obligations change; verify "
            "with the named authority before relying on any item.</i>")
        disclaimer.set_xalign(0)
        disclaimer.set_line_wrap(True)
        disclaimer.get_style_context().add_class("dim-label")
        outer.pack_start(disclaimer, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        refresh_btn = Gtk.Button(label="Refresh checks")
        refresh_btn.connect("clicked", lambda w: self._refresh_compliance())
        toolbar.pack_start(refresh_btn, False, False, 0)
        self._report_combo = Gtk.ComboBoxText()
        for rid, template in sorted((self.jurisdiction.get_report_templates().items()
                                     if self.jurisdiction is not None else [])):
            self._report_combo.append(rid, template.get("title", rid))
        if self._report_combo.get_active() is None and self._report_combo.get_model() is not None:
            try:
                self._report_combo.set_active(0)
            except Exception:
                pass
        toolbar.pack_start(Gtk.Label(label="Regulatory report:", xalign=0), False, False, 6)
        toolbar.pack_start(self._report_combo, False, False, 0)
        export_btn = Gtk.Button(label="Export report…")
        export_btn.connect("clicked", lambda w: self._export_regulatory_report())
        toolbar.pack_start(export_btn, False, False, 0)
        outer.pack_start(toolbar, False, False, 0)

        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.pack_start(panes, True, True, 0)

        # Advisory checklist
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_lbl = Gtk.Label(xalign=0)
        left_lbl.set_markup("<b>Advisory checklist</b>")
        left.pack_start(left_lbl, False, False, 0)
        self._compliance_check_store = Gtk.ListStore(str, str, str, str)
        check_tv = Gtk.TreeView(model=self._compliance_check_store)
        for i, n in [(0, "Area"), (1, "Item"), (2, "Authority"), (3, "Status")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            check_tv.append_column(c)
        check_sw = Gtk.ScrolledWindow()
        check_sw.set_vexpand(True)
        check_sw.add(check_tv)
        left.pack_start(check_sw, True, True, 0)
        panes.pack_start(left, True, True, 0)

        # Live findings
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._compliance_findings_lbl = Gtk.Label(xalign=0)
        self._compliance_findings_lbl.set_markup("<b>Current records to review</b>")
        right.pack_start(self._compliance_findings_lbl, False, False, 0)
        self._compliance_find_store = Gtk.ListStore(str, str, str, str)
        find_tv = Gtk.TreeView(model=self._compliance_find_store)
        for i, n in [(0, "Level"), (1, "Record"), (2, "Finding"), (3, "Suggested action")]:
            c = Gtk.TreeViewColumn(n, Gtk.CellRendererText(), text=i)
            c.set_resizable(True)
            find_tv.append_column(c)
        find_sw = Gtk.ScrolledWindow()
        find_sw.set_vexpand(True)
        find_sw.add(find_tv)
        right.pack_start(find_sw, True, True, 0)
        panes.pack_start(right, True, True, 0)

        self._refresh_compliance()
        return outer

    def _refresh_compliance(self):
        rs = self.jurisdiction
        if rs is None:
            self._compliance_header.set_text(
                "Jurisdiction rulesets are unavailable. Default advisory checks are not active.")
            return
        ctx = self._jurisdiction_context()
        tax_name = self.config.get("tax_regime", getattr(rs, "tax_name", "Tax"))
        tax_rate = float(self.config.get("tax_rate", getattr(rs, "tax_rate", 0.0)) or 0.0)
        authorities = ", ".join(a.get("name", "") for a in rs.authorities[:3])
        self._compliance_header.set_markup(
            "<b><big>%s (%s)</big></b>\n"
            "Ruleset %s · version %s · last reviewed %s\n"
            "Currency %s · Tax preset: %s · Date format %s\n"
            "Verify with: %s"
            % (GLib.markup_escape_text(rs.name), GLib.markup_escape_text(rs.country_code),
               GLib.markup_escape_text(rs.id), GLib.markup_escape_text(rs.ruleset_version or "1.0.0"),
               GLib.markup_escape_text(rs.updated_at or "unknown"),
               GLib.markup_escape_text(rs.currency),
               GLib.markup_escape_text(tax_preset_label(rs, tax_rate, tax_name)),
               GLib.markup_escape_text(rs.date_format),
               GLib.markup_escape_text(authorities or "the relevant authority")))

        # Checklist
        self._compliance_check_store.clear()
        try:
            items = rs.get_checklist(ctx, "general")
        except Exception as e:
            logger.warning("Jurisdiction checklist failed: %s", e)
            items = []
        if not self.config.get("verify_tax_rate", True):
            items = [i for i in items if getattr(i, "code", "") != "verify_tax_rate"]
        for item in items:
            status = "Required (advisory)" if getattr(item, "required", False) else "Advisory"
            self._compliance_check_store.append([
                str(getattr(item, "category", "") or "general").title(),
                str(getattr(item, "title", "")),
                str(getattr(item, "authority", "")),
                status,
            ])
        if not items:
            self._compliance_check_store.append(["—", "No checklist items", "", ""])

        # Findings from live records
        self._compliance_find_store.clear()
        findings = []
        specs = [
            ("Vessel", self.db.get_vessels, Vessel.from_dict),
            ("Certification", self.db.get_certifications, Certification.from_dict),
            ("Crew member", self.db.get_crew_members, CrewMember.from_dict),
            ("Expense", self.db.get_expenses, Expense.from_dict),
            ("Task", self.db.get_tasks_with_vessels, MaintenanceTask.from_dict),
            ("Trip", self.db.get_trips, TripLog.from_dict),
        ]
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        for label, loader, factory in specs:
            try:
                rows = loader()
            except DatabaseError as e:
                logger.warning("Compliance query failed for %s: %s", label, e)
                continue
            for row in rows or []:
                try:
                    entity = factory(row)
                    warnings = rs.validate(ctx, label.lower().replace(" ", "_"), entity)
                except Exception as e:
                    logger.warning("Jurisdiction validation failed for %s: %s", label, e)
                    continue
                for warning in warnings or []:
                    ident = getattr(entity, "id", None)
                    name = (getattr(entity, "name", "") or getattr(entity, "task_name", "")
                            or getattr(entity, "course_name", "")
                            or getattr(entity, "destination", "")
                            or "")
                    findings.append((severity_rank.get(warning.severity.value, 3),
                                     "%s #%s%s" % (label, ident if ident is not None else "?",
                                                   (" — " + str(name)) if name else ""),
                                     warning.message, warning.suggestion or warning.authority or ""))
        findings.sort(key=lambda f: (f[0], f[1]))
        for _rank, record, message, suggestion in findings[:500]:
            level = {0: "Critical", 1: "Warning", 2: "Info"}.get(_rank, "Info")
            self._compliance_find_store.append([level, record, message, suggestion])
        if not findings:
            self._compliance_find_store.append(["Info", "—", "No advisory findings for the current records.", ""])
        self._compliance_findings_lbl.set_markup(
            "<b>Current records to review (%d)</b>" % len(findings))

    def _report_comments(self, title):
        rs = self.jurisdiction
        generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if rs is None:
            return ("Vessel Keeper report: %s" % title,
                    "Generated: %s" % generated,
                    "Informational export only; not a statutory filing.")
        authority = rs.authorities[0]["name"] if rs.authorities else "the relevant authority"
        tax_name = self.config.get("tax_regime", getattr(rs, "tax_name", "Tax"))
        tax_rate = float(self.config.get("tax_rate", getattr(rs, "tax_rate", 0.0)) or 0.0)
        tax_text = tax_preset_label(rs, tax_rate, tax_name)
        if self.config.get("verify_tax_rate", True):
            if tax_rate > 0:
                tax_line = ("Tax preset: %s (verify with %s)"
                            % (tax_text,
                               getattr(rs, "tax_authority", "") or authority))
            else:
                tax_line = ("Tax preset: %s (confirm the tax treatment with %s)"
                            % (tax_text,
                               getattr(rs, "tax_authority", "") or authority))
        else:
            tax_line = "Tax preset: %s (verification reminder disabled)" % tax_text
        return (
            "Vessel Keeper regulatory report: %s" % title,
            "Jurisdiction: %s (%s), ruleset %s version %s, last reviewed %s"
            % (rs.name, rs.country_code, rs.id, rs.ruleset_version or "1.0.0",
               rs.updated_at or "unknown"),
            tax_line,
            "Generated: %s" % generated,
            "Informational export only; it is not a statutory filing. Verify current "
            "requirements with %s." % authority,
            rs.disclaimer,
        )

    def _export_regulatory_report(self):
        rs = self.jurisdiction
        if rs is None:
            show_error(self.window, "Jurisdiction rulesets are not available.")
            return
        templates = rs.get_report_templates()
        rid = self._report_combo.get_active_id() if hasattr(self, "_report_combo") else None
        if not rid and templates:
            rid = sorted(templates)[0]
        template = templates.get(rid)
        if not template:
            self._info("No report selected", "Choose a regulatory report first.")
            return
        dataset = template.get("dataset")
        query = EXPORT_QUERIES.get(dataset)
        if not query:
            show_error(self.window, "The report's dataset is not available in this version.")
            return
        chooser = Gtk.FileChooserDialog(title="Save %s" % template.get("title", rid),
                                        transient_for=self.window,
                                        action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name("%s_%s.csv" % (rid, datetime.date.today().isoformat()))
        try:
            if chooser.run() == Gtk.ResponseType.OK:
                path = chooser.get_filename()
                if not path:
                    return
                try:
                    count = self.db.export_csv(path, query,
                                               comments=self._report_comments(
                                                   template.get("title", rid)))
                except (ValidationError, DatabaseError, OSError) as e:
                    show_error(self.window, str(e))
                    return
                if count:
                    self._info("Report exported",
                               "%d rows written to\n%s\n\nThis export is informational "
                               "only; verify it against the authority's current "
                               "requirements." % (count, path))
                else:
                    self._info("Nothing to export",
                               "The %s dataset is empty." % template.get("title", rid))
        finally:
            chooser.destroy()

    # =========================================================================
    # CRUD HANDLERS
    # =========================================================================
    def _dialog_save(self, dlg, getter, save_fn, msg, entity_type=None):
        try:
            obj = getter()
            save_fn(obj)
            self.refresh.schedule()
            self._status(msg)
            try:
                dlg.hide()
            except Exception:
                pass
            if entity_type is None:
                entity_type = {
                    'add_vessel': 'vessel', 'update_vessel': 'vessel',
                    'add_certification': 'certification',
                    'update_certification': 'certification',
                    'add_crew_member': 'crew_member',
                    'update_crew_member': 'crew_member',
                    'add_expense': 'expense', 'update_expense': 'expense',
                    'add_task': 'task', 'update_task': 'task',
                    'add_trip': 'trip', 'update_trip': 'trip',
                }.get(getattr(save_fn, "__name__", ""))
            self._post_save_guardrails(obj, entity_type)
            return True
        except (ValidationError, DatabaseError) as e:
            show_error(self.window, str(e))
            return False

    def _post_save_guardrails(self, obj, entity_type):
        """Show non-blocking, advisory jurisdiction warnings after a save."""
        if not entity_type or obj is None:
            return
        if not self.config.get("show_guardrails", True):
            return
        rs = self.jurisdiction
        if rs is None:
            return
        try:
            warnings = rs.validate(self._jurisdiction_context(), entity_type, obj)
        except Exception as e:
            logger.warning("Jurisdiction guard-rail check failed: %s", e)
            return
        if not warnings:
            return
        rank = {"critical": 0, "warning": 1, "info": 2}
        warnings = sorted(warnings, key=lambda w: rank.get(w.severity.value, 3))
        critical = any(w.severity.value == "critical" for w in warnings)
        message_type = Gtk.MessageType.WARNING if critical else Gtk.MessageType.INFO
        d = Gtk.MessageDialog(transient_for=self.window, flags=0, modal=True,
                              message_type=message_type, buttons=Gtk.ButtonsType.OK,
                              text="Advisory guard rails — review suggested")
        lines = []
        for w in warnings[:8]:
            line = "• [%s] %s" % (w.severity.value.upper(), w.message)
            if w.suggestion:
                line += "\n   Suggested: %s" % w.suggestion
            if w.authority:
                line += "\n   Authority: %s" % w.authority
            lines.append(line)
        lines.append("")
        lines.append("These checks are informational only and are not legal advice. "
                     "Verify current requirements with %s."
                     % (rs.authorities[0]["name"] if rs.authorities else "the relevant authority"))
        d.format_secondary_text("\n".join(lines))
        d.run()
        d.destroy()
        if hasattr(self, "_compliance_find_store"):
            self._refresh_compliance()

    def _precheck(self, fn, msg):
        try:
            if fn():
                return True
        except DatabaseError as e:
            show_error(self.window, str(e))
            return False
        show_error(self.window, msg)
        return False

    def _add_vessel(self):
        d = VesselDialog(self.window, jurisdiction=self.jurisdiction)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_vessel, self.db.add_vessel, "Vessel added")
        finally:
            d.destroy()

    def _edit_vessel(self):
        vid = self._selected_id('vessels')
        if vid is None:
            self._status("Select a vessel first")
            return
        try:
            row = self.db.get_vessel(vid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = VesselDialog(self.window, Vessel.from_dict(row), jurisdiction=self.jurisdiction)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_vessel, self.db.update_vessel, "Vessel updated")
        finally:
            d.destroy()

    def _del_vessel(self):
        vid = self._selected_id('vessels')
        if vid is None:
            return
        if not confirm(self.window, "Delete this vessel and all of its tasks, trips and fuel logs?"):
            return
        try:
            self.db.delete_vessel(vid)
            self.refresh.schedule()
            self._status("Vessel deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_task(self):
        if not self._precheck(self.db.get_vessels, "Add a vessel before creating maintenance tasks."):
            return
        d = MaintenanceDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_task, self.db.add_task, "Maintenance task added")
        finally:
            d.destroy()

    def _edit_task(self):
        tid = self._selected_id('maintenance')
        if tid is None:
            self._status("Select a task first")
            return
        try:
            row = self.db.get_task(tid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = MaintenanceDialog(self.window, self.db, MaintenanceTask.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_task, self.db.update_task, "Task updated")
        finally:
            d.destroy()

    def _del_task(self):
        tid = self._selected_id('maintenance')
        if tid is None:
            return
        if not confirm(self.window, "Delete this maintenance task?"):
            return
        try:
            self.db.delete_task(tid)
            self.refresh.schedule()
            self._status("Task deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _save_item(self, data):
        if data['id']:
            return self.db.update_item(data['id'], data['name'], data['part_number'], data['category'],
                                       data['current_stock'], data['minimum_stock'],
                                       data['unit_cost'], data['supplier_id'])
        return self.db.add_item(data['name'], data['part_number'], data['category'],
                                data['current_stock'], data['minimum_stock'],
                                data['unit_cost'], data['supplier_id'])

    def _add_item(self):
        d = InventoryDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_data, self._save_item, "Inventory item added")
        finally:
            d.destroy()

    def _edit_item(self):
        iid = self._selected_id('inventory')
        if iid is None:
            self._status("Select an item first")
            return
        try:
            item = self.db.get_item(iid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not item:
            return
        d = InventoryDialog(self.window, self.db, item)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_data, self._save_item, "Inventory item updated")
        finally:
            d.destroy()

    def _del_item(self):
        iid = self._selected_id('inventory')
        if iid is None:
            return
        if not confirm(self.window, "Delete this inventory item?"):
            return
        try:
            self.db.delete_item(iid)
            self.refresh.schedule()
            self._status("Inventory item deleted")
        except DatabaseError as e:
            show_error(self.window, "Cannot delete item (it may be used by a task): %s" % e)

    def _add_trip(self):
        if not self._precheck(self.db.get_vessels, "Add a vessel before logging trips."):
            return
        d = TripDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_trip, self.db.add_trip, "Trip added")
        finally:
            d.destroy()

    def _edit_trip(self):
        tid = self._selected_id('trips')
        if tid is None:
            return
        try:
            row = next((t for t in self.db.get_trips() if t['id'] == tid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = TripDialog(self.window, self.db, TripLog.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_trip, self.db.update_trip, "Trip updated")
        finally:
            d.destroy()

    def _del_trip(self):
        tid = self._selected_id('trips')
        if tid is None:
            return
        if not confirm(self.window, "Delete this trip?"):
            return
        try:
            self.db.delete_trip(tid)
            self.refresh.schedule()
            self._status("Trip deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_supplier(self):
        d = SupplierDialog(self.window)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_supplier, self.db.add_supplier, "Supplier added")
        finally:
            d.destroy()

    def _edit_supplier(self):
        sid = self._selected_id('suppliers')
        if sid is None:
            return
        try:
            row = next((s for s in self.db.get_suppliers() if s['id'] == sid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = SupplierDialog(self.window, Supplier.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_supplier, self.db.update_supplier, "Supplier updated")
        finally:
            d.destroy()

    def _del_supplier(self):
        sid = self._selected_id('suppliers')
        if sid is None:
            return
        if not confirm(self.window, "Delete this supplier?"):
            return
        try:
            self.db.delete_supplier(sid)
            self.refresh.schedule()
            self._status("Supplier deleted")
        except DatabaseError as e:
            show_error(self.window, "Cannot delete supplier: %s" % e)

    def _add_expense(self):
        opts = self._jurisdiction_options()
        d = ExpenseDialog(self.window, self.db,
                          categories=opts.get("categories"),
                          currencies=opts.get("currencies"),
                          default_currency=opts.get("currency"),
                          default_tax_rate=opts.get("default_tax_rate"),
                          tax_regime=opts.get("tax_regime"))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_expense, self.db.add_expense, "Expense added")
        finally:
            d.destroy()

    def _edit_expense(self):
        eid = self._selected_id('expenses')
        if eid is None:
            return
        try:
            row = self.db.get_expense(eid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        opts = self._jurisdiction_options()
        d = ExpenseDialog(self.window, self.db, Expense.from_dict(row),
                          categories=opts.get("categories"),
                          currencies=opts.get("currencies"),
                          default_currency=opts.get("currency"),
                          default_tax_rate=opts.get("default_tax_rate"),
                          tax_regime=opts.get("tax_regime"))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_expense, self.db.update_expense, "Expense updated")
        finally:
            d.destroy()

    def _del_expense(self):
        eid = self._selected_id('expenses')
        if eid is None:
            return
        if not confirm(self.window, "Delete this expense?"):
            return
        try:
            self.db.delete_expense(eid)
            self.refresh.schedule()
            self._status("Expense deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_budget(self):
        if not self._precheck(self.db.get_vessels, "Add a vessel before creating budgets."):
            return
        opts = self._jurisdiction_options()
        d = BudgetDialog(self.window, self.db, categories=opts.get("categories"),
                         currency=opts.get("currency"))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_budget, self.db.add_budget, "Budget added")
        finally:
            d.destroy()

    def _edit_budget(self):
        bid = self._selected_id('budgets')
        if bid is None:
            return
        try:
            row = next((b for b in self.db.get_budgets() if b['id'] == bid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        opts = self._jurisdiction_options()
        d = BudgetDialog(self.window, self.db, Budget.from_dict(row),
                         categories=opts.get("categories"), currency=opts.get("currency"))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_budget, self.db.update_budget, "Budget updated")
        finally:
            d.destroy()

    def _del_budget(self):
        bid = self._selected_id('budgets')
        if bid is None:
            return
        if not confirm(self.window, "Delete this budget?"):
            return
        try:
            self.db.delete_budget(bid)
            self.refresh.schedule()
            self._status("Budget deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_fuel(self):
        if not self._precheck(self.db.get_vessels, "Add a vessel before logging fuel."):
            return
        d = FuelLogDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_fuel_log, self.db.add_fuel_log, "Fuel log added")
        finally:
            d.destroy()

    def _edit_fuel(self):
        fid = self._selected_id('fuel')
        if fid is None:
            return
        try:
            row = next((f for f in self.db.get_fuel_logs() if f['id'] == fid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = FuelLogDialog(self.window, self.db, FuelLog.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_fuel_log, self.db.update_fuel_log, "Fuel log updated")
        finally:
            d.destroy()

    def _del_fuel(self):
        fid = self._selected_id('fuel')
        if fid is None:
            return
        if not confirm(self.window, "Delete this fuel log?"):
            return
        try:
            self.db.delete_fuel_log(fid)
            self.refresh.schedule()
            self._status("Fuel log deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_crew(self):
        d = CrewDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_crew_member, self.db.add_crew_member, "Crew member added")
        finally:
            d.destroy()

    def _edit_crew(self):
        cid = self._selected_id('crew')
        if cid is None:
            return
        try:
            row = self.db.get_crew_member(cid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = CrewDialog(self.window, self.db, CrewMember.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_crew_member, self.db.update_crew_member, "Crew member updated")
        finally:
            d.destroy()

    def _del_crew(self):
        cid = self._selected_id('crew')
        if cid is None:
            return
        if not confirm(self.window, "Delete this crew member with all certifications and training records?"):
            return
        try:
            self.db.delete_crew_member(cid)
            self.refresh.schedule()
            self._status("Crew member deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _manage_crew_certs(self):
        cid = self._selected_id('crew')
        if cid is None:
            self._status("Select a crew member first")
            return
        try:
            row = self.db.get_crew_member(cid)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        opts = self._jurisdiction_options()
        dlg = CertDialog(self.window, self.db, cid, "%s %s" % (row['first_name'], row['last_name']),
                         presets=opts.get("cert_presets"),
                         categories=opts.get("cert_categories"),
                         default_issuing_body=opts.get("issuing_body", ""),
                         on_saved=lambda cert: self._post_save_guardrails(cert, "certification"))
        try:
            dlg.run()
        finally:
            dlg.destroy()
        self.refresh.schedule()

    def _add_cert(self):
        if not self._precheck(self.db.get_crew_members, "Add a crew member before recording certifications."):
            return
        opts = self._jurisdiction_options()
        d = CertificationDialog(self.window, self.db,
                                presets=opts.get("cert_presets"),
                                categories=opts.get("cert_categories"),
                                default_issuing_body=opts.get("issuing_body", ""))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_certification, self.db.add_certification, "Certification added")
        finally:
            d.destroy()

    def _edit_cert(self):
        cid = self._selected_id('certifications')
        if cid is None:
            return
        try:
            row = next((c for c in self.db.get_certifications() if c['id'] == cid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        opts = self._jurisdiction_options()
        d = CertificationDialog(self.window, self.db, Certification.from_dict(row),
                                presets=opts.get("cert_presets"),
                                categories=opts.get("cert_categories"),
                                default_issuing_body=opts.get("issuing_body", ""))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_certification, self.db.update_certification, "Certification updated")
        finally:
            d.destroy()

    def _del_cert(self):
        cid = self._selected_id('certifications')
        if cid is None:
            return
        if not confirm(self.window, "Delete this certification?"):
            return
        try:
            self.db.delete_certification(cid)
            self.refresh.schedule()
            self._status("Certification deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_training(self):
        if not self._precheck(self.db.get_crew_members, "Add a crew member before recording training."):
            return
        d = TrainingDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_training_record, self.db.add_training_record, "Training record added")
        finally:
            d.destroy()

    def _edit_training(self):
        tid = self._selected_id('training')
        if tid is None:
            return
        try:
            row = next((t for t in self.db.get_training_records() if t['id'] == tid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = TrainingDialog(self.window, self.db, TrainingRecord.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_training_record, self.db.update_training_record, "Training record updated")
        finally:
            d.destroy()

    def _del_training(self):
        tid = self._selected_id('training')
        if tid is None:
            return
        if not confirm(self.window, "Delete this training record?"):
            return
        try:
            self.db.delete_training_record(tid)
            self.refresh.schedule()
            self._status("Training record deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _add_watch(self):
        if not self._precheck(self.db.get_crew_members, "Add a crew member before scheduling watches."):
            return
        d = WatchScheduleDialog(self.window, self.db)
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_watch_schedule, self.db.add_watch_schedule, "Watch scheduled")
        finally:
            d.destroy()

    def _edit_watch(self):
        wid = self._selected_id('watch')
        if wid is None:
            return
        try:
            row = next((w for w in self.db.get_watch_schedules() if w['id'] == wid), None)
        except DatabaseError as e:
            show_error(self.window, str(e))
            return
        if not row:
            return
        d = WatchScheduleDialog(self.window, self.db, WatchSchedule.from_dict(row))
        try:
            if d.run() == Gtk.ResponseType.OK:
                self._dialog_save(d, d.get_watch_schedule, self.db.update_watch_schedule, "Watch updated")
        finally:
            d.destroy()

    def _del_watch(self):
        wid = self._selected_id('watch')
        if wid is None:
            return
        if not confirm(self.window, "Delete this watch entry?"):
            return
        try:
            self.db.delete_watch_schedule(wid)
            self.refresh.schedule()
            self._status("Watch entry deleted")
        except DatabaseError as e:
            show_error(self.window, str(e))

    def _del_stock(self):
        tid = self._selected_id('stock')
        if tid is None:
            return
        if not confirm(self.window, "Delete this stock transaction and reverse its effect on stock?"):
            return
        try:
            self.db.delete_stock_transaction(tid)
            self.refresh.schedule()
            self._status("Stock transaction reversed")
        except DatabaseError as e:
            show_error(self.window, str(e))

    # =========================================================================
    # RENDERING & LIFECYCLE
    # =========================================================================
    def _apply_css(self):
        try:
            screen = Gdk.Screen.get_default()
            if screen is None:
                return
            css = b"""
            .card-title { font-size: 11px; font-weight: bold; color: #ffffff; }
            .card-value { font-size: 24px; font-weight: bold; color: #ffffff; }
            .card-subtitle { font-size: 10px; color: #f0f0f0; }
            .dim-label { color: #666666; font-size: 11px; }
            .summary-value { font-weight: bold; font-size: 13px; }
            treeview { font-size: 12px; }
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(screen, provider,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception as e:
            logger.warning("CSS setup failed: %s", e)

    def _restore_pos(self):
        try:
            x = int(self.config.get("window_x", 100))
            y = int(self.config.get("window_y", 100))
            w = int(self.config.get("window_width", 1200))
            h = int(self.config.get("window_height", 800))
            self.window.move(x, y)
            self.window.resize(max(900, w), max(600, h))
        except (TypeError, ValueError):
            pass

    def _update_views(self, vessels, items, tasks, inv_summary, task_counts, vessel_count,
                      trips, suppliers, stock, expenses, finsum, feff, crew, crew_counts,
                      certifications, templates=None, training=None, watch=None,
                      budgets=None, fuel_logs=None):
        if self._shutting:
            return
        training = training or []
        watch = watch or []
        budgets = budgets or []
        fuel_logs = fuel_logs or []

        self._set_rows('vessels', [
            [v['id'], v.get('name') or '', v.get('vessel_type') or '',
             ("%s %s" % (v.get('engine_make') or '', v.get('engine_model') or '')).strip(),
             v.get('current_location') or '', float(v.get('engine_hours') or 0.0),
             v.get('flag_state') or '', v.get('port_of_registry') or '',
             v.get('official_number') or '', v.get('imo_number') or '',
             v.get('registration_expiry') or '']
            for v in vessels])

        sup_names = {s['id']: s['name'] for s in suppliers}
        self._set_rows('inventory', [
            [i['id'], i.get('name') or '', i.get('part_number') or '', i.get('category') or '',
             int(i.get('current_stock') or 0), int(i.get('minimum_stock') or 0),
             float(i.get('unit_cost') or 0.0), sup_names.get(i.get('supplier_id'), '')]
            for i in items])

        self._set_rows('maintenance', [
            [t['id'], t.get('vessel_name') or '', t.get('task_name') or '', t.get('system') or '',
             t.get('next_due') or '', t.get('status') or '', t.get('priority') or '',
             t.get('work_order') or '']
            for t in tasks])

        self._set_rows('trips', [
            [t['id'], t.get('vessel_name') or '', t.get('departure') or '', t.get('arrival') or '',
             t.get('destination') or '', float(t.get('distance') or 0.0), float(t.get('fuel_used') or 0.0)]
            for t in trips])

        self._set_rows('suppliers', [
            [s['id'], s.get('name') or '', s.get('contact') or '', s.get('phone') or '',
             s.get('email') or '', "Yes" if s.get('preferred') else "No"]
            for s in suppliers])

        self._set_rows('stock', [
            [s['id'], s.get('item_name') or '', s.get('transaction_type') or '',
             int(s.get('quantity') or 0), s.get('notes') or '', s.get('created_at') or '']
            for s in stock])

        self._set_rows('expenses', [
            [e['id'], e.get('expense_date') or '', e.get('vessel_name') or '—',
             e.get('category') or '', float(e.get('amount') or 0.0),
             float(e.get('gst_amount') or 0.0),
             float(e.get('amount') or 0.0) + float(e.get('gst_amount') or 0.0),
             e.get('currency') or '', e.get('vendor') or '', e.get('description') or '']
            for e in expenses])

        budget_rows = []
        for b in budgets:
            amount = float(b.get('budget_amount') or 0.0)
            spent = float(b.get('spent_amount') or 0.0)
            budget_rows.append([b['id'], b.get('vessel_name') or '', int(b.get('fiscal_year') or 0),
                                b.get('category') or '', amount, spent, amount - spent,
                                (spent / amount * 100.0) if amount > 0 else 0.0])
        self._set_rows('budgets', budget_rows)

        self._set_rows('fuel', [
            [f['id'], f.get('vessel_name') or '', f.get('created_at') or '',
             float(f.get('gallons') or 0.0), float(f.get('cost_per_gallon') or 0.0),
             float(f.get('total_cost') or 0.0), f.get('fuel_type') or '',
             " / ".join(x for x in (f.get('vendor') or '', f.get('location') or '') if x)]
            for f in fuel_logs])

        self._set_rows('crew', [
            [c['id'], c.get('first_name') or '', c.get('last_name') or '', c.get('role') or '',
             c.get('status') or '', c.get('email') or '', c.get('phone') or '']
            for c in crew])

        self._set_rows('certifications', [
            [c['id'], c.get('crew_member') or '', c.get('name') or '', c.get('issuing_body') or '',
             c.get('cert_number') or '', c.get('issue_date') or '', c.get('expiry_date') or '']
            for c in certifications])

        self._set_rows('training', [
            [t['id'], t.get('crew_member') or '', t.get('course_name') or '', t.get('provider') or '',
             t.get('date_completed') or '', t.get('expiry_date') or '', float(t.get('cost') or 0.0)]
            for t in training])

        self._set_rows('watch', [
            [w['id'], w.get('date') or '', w.get('crew_name') or '', w.get('vessel_name') or '',
             w.get('start_time') or '', w.get('end_time') or '', w.get('role_on_watch') or '']
            for w in watch])

        # Dashboard
        open_tasks = max(0, (task_counts.get('total') or 0) - (task_counts.get('completed') or 0))
        updates = {
            'vessels': str(vessel_count),
            'tasks': str(open_tasks),
            'overdue': str(task_counts.get('overdue') or 0),
            'due_soon': str(task_counts.get('due_soon') or 0),
            'low_stock': str(inv_summary.get('low_count') or 0),
            'value': format_money(inv_summary.get('total_value') or 0.0),
            'crew': str(crew_counts.get('active') or 0),
            'cert_expiry': str((crew_counts.get('expiring_certs') or 0) + (crew_counts.get('expired_certs') or 0)),
        }
        for key, val in updates.items():
            card = self._cards.get(key)
            if card is not None:
                card.update(val)

        if hasattr(self, '_dash_store'):
            self._dash_store.clear()
            active = [t for t in tasks if (t.get('status') or '') != 'completed']
            active.sort(key=lambda t: t.get('next_due') or '9999-12-31')
            for t in active[:10]:
                self._dash_store.append([t.get('vessel_name') or '', t.get('task_name') or '',
                                         t.get('next_due') or '', t.get('status') or ''])

        # Finance
        try:
            self._fin_labels['year'].set_text(str(finsum.get('year') or ''))
            self._fin_labels['total_expenses'].set_text(format_money(finsum.get('total_expenses') or 0.0))
            self._fin_labels['expense_count'].set_text(str(finsum.get('expense_count') or 0))
            self._fin_labels['total_budget'].set_text(format_money(finsum.get('total_budget') or 0.0))
            self._fin_labels['budget_spent'].set_text(format_money(finsum.get('total_budget_spent') or 0.0))
            self._fin_labels['utilization'].set_text("%.1f%%" % (finsum.get('budget_utilization_pct') or 0.0))
            self._fin_labels['fuel_cost'].set_text(format_money(finsum.get('total_fuel_cost') or 0.0))
        except Exception:
            pass
        self._fin_store.clear()
        total_spend = float(finsum.get('total_expenses') or 0.0)
        for row in finsum.get('by_category') or []:
            amt = float(row.get('total') or 0.0)
            self._fin_store.append([row.get('category') or '', amt,
                                    (amt / total_spend * 100.0) if total_spend else 0.0])
        self._feff_store.clear()
        for row in feff or []:
            self._feff_store.append([row.get('vessel_name') or '', float(row.get('total_distance') or 0.0),
                                     float(row.get('total_gallons') or 0.0),
                                     float(row.get('total_fuel_cost') or 0.0),
                                     float(row.get('nmpg') or 0.0)])

        self._status("%d vessels · %d inventory items · %d open tasks · %d crew"
                     % (vessel_count, len(items), open_tasks, crew_counts.get('active') or 0))
        try:
            if hasattr(self, "_compliance_find_store") and self._current_page_name() == "compliance":
                self._refresh_compliance()
        except Exception as e:
            logger.warning("Compliance refresh skipped: %s", e)

    # ---- backups ----
    def _manual_backup(self):
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            path = os.path.join(BACKUP_DIR, "vessel_keeper_%s.db"
                                % datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            if self.db.create_backup(path):
                self._info("Backup complete", "Database backed up to:\n%s" % path)
            else:
                show_error(self.window, "Backup failed. See the log for details.")
        except OSError as e:
            show_error(self.window, str(e))

    def _export_diagnostics_ui(self):
        dlg = Gtk.FileChooserDialog(title="Export Diagnostics Bundle", transient_for=self.window,
                                    action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Save", Gtk.ResponseType.OK)
        dlg.set_current_name("vessel_keeper_diagnostics_%s.zip" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        dlg.set_current_folder(DATA_DIR)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            out = dlg.get_filename()
            dlg.destroy()
            try:
                path = _export_diagnostics_bundle(out)
                self._info("Diagnostics exported", "Saved to:\n%s" % path)
            except Exception as e:
                show_error(self.window, str(e))
        else:
            dlg.destroy()

    def _restore_from_backup_dialog(self):
        dlg = Gtk.FileChooserDialog(title="Restore from Backup", transient_for=self.window,
                                    action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Open", Gtk.ResponseType.OK)
        try:
            filt = Gtk.FileFilter()
            filt.set_name("SQLite Databases")
            filt.add_pattern("*.db")
            filt.add_pattern("*.sqlite")
            filt.add_pattern("*.sqlite3")
            dlg.add_filter(filt)
            dlg.set_current_folder(BACKUP_DIR)
        except Exception:
            pass
        resp = dlg.run()
        if resp != Gtk.ResponseType.OK:
            dlg.destroy()
            return
        src = dlg.get_filename()
        dlg.destroy()
        if not src:
            return
        if not confirm(self.window,
                       "Restoring will replace the current database with the selected backup. "
                       "An emergency backup of the current database will be created first.\n\nContinue?",
                       "Confirm Restore"):
            return
        try:
            # Create emergency backup before restore
            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                emg = os.path.join(BACKUP_DIR, "emergency_before_restore_%s.db"
                                   % datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
                self.db.create_backup(emg)
            except Exception:
                pass
            ok = self.db.restore_from_backup(src)
            if ok:
                self._info("Restore complete",
                           "Database restored from:\n%s\n\nThe application will now shut down. "
                           "Please restart it." % src)
                self._shutdown()
            else:
                show_error(self.window, "Restore failed. See the log for details.")
        except Exception as e:
            show_error(self.window, str(e))

    def _auto_backup(self):
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            path = os.path.join(BACKUP_DIR, "vessel_keeper_%s.db"
                                % datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            if not self.db.create_backup(path):
                logger.warning("Automatic backup failed")
        except OSError as e:
            logger.warning("Automatic backup error: %s", e)

    def _cleanup_backups(self):
        try:
            days = max(1, int(self.config.get("backup_retention_days", 30)))
        except (TypeError, ValueError):
            days = 30
        cutoff = time.time() - days * 86400
        try:
            for p in Path(BACKUP_DIR).glob("*.db"):
                if p.name.startswith("emergency"):
                    continue
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        logger.info("Removed old backup %s", p)
                except OSError:
                    pass
        except OSError as e:
            logger.warning("Backup cleanup failed: %s", e)

    def _newest_backup_mtime(self):
        try:
            files = list(Path(BACKUP_DIR).glob("*.db"))
            return max((p.stat().st_mtime for p in files), default=0.0)
        except OSError:
            return 0.0

    def _start_bg(self):
        self._bg_stop.clear()
        t = threading.Thread(target=self._bg_loop, name="vk-background", daemon=True)
        self._bg_thread = t
        t.start()

    def _bg_loop(self):
        last_backup = self._newest_backup_mtime()
        last_cleanup = 0.0
        while not self._bg_stop.wait(60):
            if self._shutting:
                break
            try:
                if self.config.get("auto_backup", True):
                    try:
                        interval = max(1, int(self.config.get("backup_interval_hours", 24))) * 3600
                    except (TypeError, ValueError):
                        interval = 86400
                    if time.time() - last_backup >= interval:
                        self._auto_backup()
                        last_backup = time.time()
                if time.time() - last_cleanup > 86400:
                    self._cleanup_backups()
                    last_cleanup = time.time()
            except Exception as e:
                logger.error("Background task failed: %s", e, exc_info=True)

    # ---- shutdown ----
    def _shutdown(self, *args):
        """Leave the GTK loop quickly; cleanup happens in finalize().

        Everything here must be fast. The previous implementation saved the
        config and closed the database (including a blocking WAL checkpoint)
        from the GTK main thread before quitting, so a reader or slow disk
        froze the window. A watchdog guarantees the process exits even if a
        worker thread is wedged in SQLite.
        """
        if self._shutting:
            return False
        self._shutting = True
        _EXITING.set()
        try:
            self.ui.request_shutdown()
        except Exception:
            pass
        # Hide first so the window disappears immediately.
        try:
            self.window.hide()
        except Exception:
            pass
        self._bg_stop.set()
        # Guarantee termination even if a nested modal loop refuses to unwind
        # or a worker thread blocks. When shutdown happens before Gtk.main()
        # starts, main() exits hard itself, so no watchdog is needed here.
        if Gtk.main_level() > 0:
            try:
                threading.Thread(target=self._exit_watchdog, name="vk-exit-watchdog",
                                 daemon=True).start()
            except Exception:
                pass
        if self._session_timer_id:
            try:
                GLib.source_remove(self._session_timer_id)
            except Exception:
                pass
            self._session_timer_id = None
        # Quit the innermost loop and keep quitting as nested loops (dialog
        # run() calls) unwind, so the app cannot get stuck in a modal loop.
        try:
            GLib.idle_add(self._quit_gtk)
        except Exception:
            pass
        self._quit_gtk()
        return False

    def _exit_watchdog(self, timeout=4.0):
        time.sleep(timeout)
        _hard_exit(0)

    def _quit_gtk(self):
        try:
            if Gtk.main_level() > 0:
                Gtk.main_quit()
                return True
        except Exception:
            pass
        return False

    def finalize(self):
        """Persist state and release resources after the GTK loop has stopped."""
        if self._finalized:
            return
        self._finalized = True
        try:
            self.refresh.stop()
        except Exception:
            pass
        try:
            w, h = self.window.get_size()
            x, y = self.window.get_position()
            self.config.set("window_width", w)
            self.config.set("window_height", h)
            self.config.set("window_x", x)
            self.config.set("window_y", y)
            self.config.save()
        except Exception as e:
            logger.warning("Could not save window geometry: %s", e)
        if (self._bg_thread is not None and self._bg_thread.is_alive()
                and self._bg_thread is not threading.current_thread()):
            self._bg_thread.join(timeout=0.5)
        try:
            self.db.close()
        except Exception as e:
            logger.warning("Database close failed: %s", e)

    def _on_delete_window(self, *args):
        self._shutdown()
        return True

class CertificationDialog(EntityDialog):
    CATEGORIES = ["STCW", "Safety", "Engineering", "Medical", "Navigation", "Security", "GMDSS", "Other"]

    def __init__(self, parent, db, cert=None, crew_id=None, presets=None, categories=None,
                 default_issuing_body=""):
        self._presets = [dict(p) for p in (presets or []) if p.get("name")]
        cats = list(dict.fromkeys((categories or []) + self.CATEGORIES))
        super().__init__(parent, "Edit Certification" if cert and cert.id else "Add Certification",
                         [("crew_member_id", "Crew Member *", "combo"),
                          ("name", "Certification Name *", 'combo_entry'),
                          ("issuing_body", "Issuing Body", str),
                          ("cert_number", "Certificate Number", str),
                          ("category", "Category", "combo"),
                          ("issue_date", "Issue Date (%s)" % date_input_hint(), str),
                          ("expiry_date", "Expiry Date (%s)" % date_input_hint(), str)])
        self._db = db
        self._cid = cert.id if cert else None
        self._default_issuing_body = default_issuing_body
        self._categories = cats
        crew = db.get_crew_members()
        self._cmap = {("%s %s" % (c['first_name'], c['last_name'])): c['id'] for c in crew}
        self._rcmap = {v: k for k, v in self._cmap.items()}
        names = list(self._cmap.keys())
        self.set_combo("crew_member_id", names)
        self.set_combo("category", cats)
        self.set_combo("name", [p["name"] for p in self._presets], "")
        if self._presets:
            preset_combo = Gtk.ComboBoxText()
            preset_combo.append("", "Choose a suggested certificate…")
            for i, p in enumerate(self._presets):
                preset_combo.append(str(i), p["name"])
            preset_combo.set_active(0)
            preset_combo.connect("changed", self._apply_preset, preset_combo)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label="Suggested:", xalign=0), False, False, 0)
            row.pack_start(preset_combo, True, True, 0)
            self.get_content_area().pack_start(row, False, False, 0)
        if cert:
            self.set_combo("crew_member_id", names, self._rcmap.get(cert.crew_member_id, ""))
            self.set_combo("name", [p["name"] for p in self._presets], cert.name)
            self.set_val("issuing_body", cert.issuing_body)
            self.set_val("cert_number", cert.cert_number)
            self.set_combo("category", cats, cert.category)
            self.set_val("issue_date", format_date(cert.issue_date) if cert.issue_date else "")
            self.set_val("expiry_date", format_date(cert.expiry_date) if cert.expiry_date else "")
        elif crew_id:
            self.set_combo("crew_member_id", names, self._rcmap.get(crew_id, ""))
            if default_issuing_body:
                self.set_val("issuing_body", default_issuing_body)
        elif default_issuing_body:
            self.set_val("issuing_body", default_issuing_body)

    def _apply_preset(self, combo, _data=None):
        idx = combo.get_active_id()
        if idx in (None, ""):
            return
        try:
            preset = self._presets[int(idx)]
        except (IndexError, ValueError):
            return
        self.set_val("name", preset["name"])
        if preset.get("issuing_body"):
            self.set_val("issuing_body", preset["issuing_body"])
        if preset.get("category"):
            self.set_combo("category", self._categories, preset["category"])

    def get_certification(self):
        cname = self.get_val("crew_member_id")
        return Certification(id=self._cid, crew_member_id=self._cmap.get(cname, 0),
                             name=self.get_val("name"), issuing_body=self.get_val("issuing_body"),
                             cert_number=self.get_val("cert_number"), category=self.get_val("category"),
                             issue_date=self.get_val("issue_date"),
                             expiry_date=self.get_val("expiry_date"))


# =============================================================================
# UTILITIES
# =============================================================================
def show_error(parent, message, title="Error"):
    """Show a modal error dialog. Safe to call from worker threads."""
    if threading.current_thread() is not threading.main_thread():
        GLib.idle_add(show_error, parent, message, title)
        return
    win = parent if isinstance(parent, Gtk.Window) else None
    if win is None and parent is not None and hasattr(parent, "get_toplevel"):
        top = parent.get_toplevel()
        if isinstance(top, Gtk.Window):
            win = top
    try:
        dlg = Gtk.MessageDialog(transient_for=win, flags=0, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(str(message))
        dlg.run()
        dlg.destroy()
    except Exception as e:
        logger.error("Could not display error dialog (%s): %s", e, message)


def confirm(parent, message, title="Confirm"):
    if threading.current_thread() is not threading.main_thread():
        GLib.idle_add(confirm, parent, message, title)
        return False
    win = parent if isinstance(parent, Gtk.Window) else None
    if win is None and parent is not None and hasattr(parent, "get_toplevel"):
        top = parent.get_toplevel()
        if isinstance(top, Gtk.Window):
            win = top
    dlg = Gtk.MessageDialog(transient_for=win, flags=0, modal=True,
                            message_type=Gtk.MessageType.QUESTION,
                            buttons=Gtk.ButtonsType.YES_NO, text=title)
    dlg.format_secondary_text(str(message))
    resp = dlg.run()
    dlg.destroy()
    return resp == Gtk.ResponseType.YES


class SingleInstanceLock:
    def __init__(self, name="vessel-keeper"):
        self.name = name
        self.lock_path = os.path.join(DATA_DIR, ".%s.lock" % name)
        self.fd = None
        self.acquired = False

    def acquire(self):
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        except Exception:
            pass
        if fcntl is not None:
            try:
                self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(self.fd, str(os.getpid()).encode())
                self.acquired = True
                return True
            except (IOError, OSError):
                return False
        elif msvcrt is not None:
            try:
                self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                os.lseek(self.fd, 0, os.SEEK_SET)
                os.write(self.fd, str(os.getpid()).encode())
                self.acquired = True
                return True
            except (IOError, OSError):
                return False
        # No locking available on this platform - proceed
        self.acquired = True
        return True

    def release(self):
        if not self.acquired:
            return
        try:
            if fcntl is not None and self.fd is not None:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            if msvcrt is not None and self.fd is not None:
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass
        try:
            if os.path.exists(self.lock_path):
                os.unlink(self.lock_path)
        except Exception:
            pass


def _hard_exit(code=0):
    """Flush streams and terminate without running interpreter finalization.

    Python cannot safely finalize while daemon worker threads are still inside
    SQLite or logging: it raises ``_enter_buffered_busy`` or hangs waiting for
    a buffered stream lock. ``os._exit`` bypasses that race, which is the
    difference between a clean quit and a process that has to be killed.
    """
    _EXITING.set()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        for handler in logging.getLogger().handlers:
            handler.flush()
    except Exception:
        pass
    os._exit(code)


def setup_excepthook():
    def handler(exc_type, exc_value, exc_tb):
        if _EXITING.is_set():
            return
        logger.error("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        if threading.current_thread() is threading.main_thread():
            try:
                show_error(None, "%s: %s" % (exc_type.__name__, exc_value), "Unexpected Error")
            except Exception:
                pass

    sys.excepthook = handler
    try:
        threading.excepthook = lambda args: handler(args.exc_type, args.exc_value, args.exc_traceback)
    except AttributeError:
        pass


def _install_signal_handlers(app):
    """Route Ctrl+C / SIGTERM through the normal shutdown path."""
    def handler(signum, frame):
        try:
            GLib.idle_add(app._shutdown)
        except Exception:
            pass
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not on the main thread / unsupported platform


def _print_version():
    print("%s %s (build %s)" % (APP_NAME, APP_VERSION, APP_BUILD))
    print("Data dir: %s" % DATA_DIR)
    print("Config dir: %s" % CONFIG_DIR)


def _export_diagnostics_bundle(out_path=None):
    try:
        Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(CONFIG_DIR).mkdir(parents=True, exist_ok=True)
        if out_path is None:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(DATA_DIR, "diagnostics_%s.zip" % ts)
        out_path = os.path.abspath(out_path)
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            info = []
            info.append("app: %s %s build %s" % (APP_NAME, APP_VERSION, APP_BUILD))
            info.append("python: %s" % sys.version)
            info.append("platform: %s" % platform.platform())
            info.append("pid: %s" % os.getpid())
            info.append("user: %s" % getpass.getuser())
            info.append("display: %s" % os.environ.get("DISPLAY", ""))
            info.append("config_dir: %s" % CONFIG_DIR)
            info.append("data_dir: %s" % DATA_DIR)
            info.append("database: %s" % DATABASE_FILE)
            info.append("log_dir: %s" % LOG_DIR)
            zf.writestr("version.txt", "\n".join(info) + "\n")
            # Redacted config
            try:
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                    redacted = dict(cfg)
                    for k in list(redacted.keys()):
                        if 'pin' in k.lower() or 'hash' in k.lower() or 'password' in k.lower():
                            redacted[k] = "***REDACTED***"
                    zf.writestr("config_redacted.json", json.dumps(redacted, indent=2) + "\n")
            except Exception as e:
                zf.writestr("config_redacted.json", "ERROR: %s\n" % e)
            # Logs (tail)
            try:
                log_path = os.path.join(LOG_DIR, "vessel_keeper.log")
                if os.path.exists(log_path):
                    with open(log_path) as f:
                        content = f.read()
                    if len(content) > 100000:
                        content = content[-100000:]
                    zf.writestr("vessel_keeper.log.tail", content)
            except Exception as e:
                zf.writestr("vessel_keeper.log.tail", "ERROR: %s\n" % e)
            # Schema/counts if possible
            try:
                if os.path.exists(DATABASE_FILE):
                    conn = sqlite3.connect(DATABASE_FILE)
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [r[0] for r in cur.fetchall()]
                    rows = []
                    rows.append("tables: %s" % tables)
                    for t in tables:
                        try:
                            cur.execute("SELECT COUNT(*) FROM %s" % t)
                            rows.append("%s: %s" % (t, cur.fetchone()[0]))
                        except Exception as e:
                            rows.append("%s: ERROR %s" % (t, e))
                    try:
                        cur.execute("PRAGMA integrity_check")
                        rows.append("integrity_check: %s" % cur.fetchone()[0])
                    except Exception as e:
                        rows.append("integrity_check: ERROR %s" % e)
                    conn.close()
                    zf.writestr("db_info.txt", "\n".join(rows) + "\n")
            except Exception as e:
                zf.writestr("db_info.txt", "ERROR: %s\n" % e)
        return out_path
    except Exception as e:
        raise


def main():
    args = sys.argv[1:]
    if "--version" in args or "-v" in args:
        _print_version()
        return 0
    if "--help" in args or "-h" in args:
        print("%s %s (build %s)" % (APP_NAME, APP_VERSION, APP_BUILD))
        print("Usage: vessel-keeper [--version] [--help] [--diagnostics [FILE]] [--allow-multiple]")
        print("Options:")
        print("  --version        Show version and exit")
        print("  --help           Show this help and exit")
        print("  --diagnostics    Export diagnostics bundle and exit")
        print("  --allow-multiple Allow multiple instances (not recommended)")
        return 0
    if "--diagnostics" in args:
        out = None
        try:
            idx = args.index("--diagnostics")
            if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                out = args[idx + 1]
        except Exception:
            pass
        try:
            path = _export_diagnostics_bundle(out)
            print("Diagnostics written: %s" % path)
            return 0
        except Exception as e:
            print("Failed to export diagnostics: %s" % e, file=sys.stderr)
            return 1

    allow_multi = "--allow-multiple" in args
    if not allow_multi:
        _inst_lock = SingleInstanceLock(APP_SLUG)
        if not _inst_lock.acquire():
            print("Another instance of %s is already running." % APP_NAME, file=sys.stderr)
            print("Use --allow-multiple to override (may cause database locks).", file=sys.stderr)
            return 1
    else:
        _inst_lock = None

    try:
        Gtk.init(None)
    except Exception:
        pass
    setup_excepthook()
    try:
        app = VesselKeeperApp()
    except Exception as e:
        logger.error("Startup failed: %s", e, exc_info=True)
        show_error(None, "Vessel Keeper could not start:\n\n%s" % e, "Startup Error")
        _hard_exit(1)
    if getattr(app, "_shutting", False):
        # The PIN gate already decided not to open the window.
        app.finalize()
        _hard_exit(0)
    _install_signal_handlers(app)
    try:
        Gtk.main()
    finally:
        try:
            threading.Thread(target=app._exit_watchdog, name="vk-exit-watchdog",
                             daemon=True).start()
        except Exception:
            pass
        app.finalize()
        if not allow_multi and _inst_lock is not None:
            try:
                _inst_lock.release()
            except Exception:
                pass
    _hard_exit(0)


if __name__ == "__main__":
    sys.exit(main())
