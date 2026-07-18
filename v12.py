import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Gdk, Gio, GLib, Pango
import sys, os, sqlite3, logging, json, csv, datetime, threading
import shutil, contextvars, uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Callable, Iterator, TypedDict
from enum import Enum, auto
from contextlib import contextmanager
from queue import Queue, Empty, Full
import time
from pathlib import Path

CONFIG_FILE = "vessel_keeper_config.json"
DATABASE_FILE = "vessel_keeper.db"
REPORTS_DIR = "reports"
BACKUP_DIR = "backups"
MAX_SCHEMA_VERSION = 3
EXPORT_QUERIES = {
    'vessels': "SELECT * FROM vessels ORDER BY name",
    'inventory': "SELECT * FROM inventory_items ORDER BY name",
    'tasks': "SELECT mt.*,v.name vessel_name FROM maintenance_tasks mt LEFT JOIN vessels v ON mt.vessel_id=v.id ORDER BY mt.next_due",
    'trips': "SELECT tl.*,v.name vessel_name FROM trip_logs tl LEFT JOIN vessels v ON tl.vessel_id=v.id ORDER BY tl.departure DESC",
    'suppliers': "SELECT * FROM suppliers ORDER BY name",
}

request_id = contextvars.ContextVar('request_id', default='system')

class VesselRow(TypedDict, total=False): id: int; name: str; vessel_type: str; engine_make: str; engine_model: str; current_location: str; created_at: str
class MaintenanceTaskRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; system: str; task_name: str; description: str; next_due: str; status: str; priority: str; created_at: str
class InventoryItemRow(TypedDict, total=False): id: int; name: str; part_number: str; category: str; current_stock: int; minimum_stock: int; unit_cost: float; supplier_id: int; created_at: str
class TripLogRow(TypedDict, total=False): id: int; vessel_id: int; vessel_name: str; departure: str; arrival: str; destination: str; distance: float; fuel_used: float; created_at: str
class SupplierRow(TypedDict, total=False): id: int; name: str; contact: str; phone: str; email: str; preferred: int; created_at: str
class StockTransactionRow(TypedDict, total=False): id: int; item_id: int; item_name: str; quantity: int; transaction_type: str; notes: str; created_at: str

class StructuredLogger:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        Path("logs").mkdir(exist_ok=True)
        logging.basicConfig(level=logging.INFO,
            format='%(asctime)s | %(name)-20s | %(levelname)-8s | %(threadName)-15s | %(message)s',
            handlers=[logging.FileHandler("logs/vessel_keeper.log", encoding='utf-8'), logging.StreamHandler(sys.stdout)])
    def info(self, m, **c): self.logger.info(f"{m} | {self._fmt(c)}")
    def error(self, m, **c): self.logger.error(f"{m} | {self._fmt(c)}")
    def warning(self, m, **c): self.logger.warning(f"{m} | {self._fmt(c)}")
    def debug(self, m, **c): self.logger.debug(f"{m} | {self._fmt(c)}")
    def critical(self, m, **c): self.logger.critical(f"{m} | {self._fmt(c)}")
    def _fmt(self, c):
        rid = request_id.get(); extra = " | ".join(f"{k}={v}" for k,v in c.items())
        return f"request_id={rid} | {extra}" if extra else f"request_id={rid}"
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
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), name=d.get('name',''), vessel_type=d.get('vessel_type',''),
                   engine_make=d.get('engine_make',''), engine_model=d.get('engine_model',''),
                   current_location=d.get('current_location',''), created_at=d.get('created_at',''))

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
    created_at: str = ""

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get('id'), vessel_id=d.get('vessel_id',0), vessel_name=d.get('vessel_name',''),
                   system=d.get('system',''), task_name=d.get('task_name',''),
                   description=d.get('description',''), next_due=d.get('next_due',''),
                   status=d.get('status','pending'), priority=d.get('priority','medium'),
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

class DatabaseError(Exception): pass
class ValidationError(Exception): pass
class ResourceError(Exception): pass

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
        if not os.path.exists(self.config_file): return
        try:
            with open(self.config_file) as f: uc = json.load(f)
            with self._lock:
                for k,v in uc.items():
                    if hasattr(self._config,k): setattr(self._config,k,v)
        except (json.JSONDecodeError,IOError) as e: logger.warning(f"Config load failed: {e}")
    def get(self,k,d=None):
        with self._lock: return getattr(self._config,k,d)
    def set(self,k,v):
        with self._lock:
            if hasattr(self._config,k): setattr(self._config,k,v)
    def save(self):
        try:
            tmp=f"{self.config_file}.tmp"
            with open(tmp,'w') as f: json.dump(dict(self._config.items()),f,indent=2)
            os.replace(tmp,self.config_file)
        except IOError as e: logger.error(f"Config save failed: {e}"); raise

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
        except sqlite3.Error as e: raise DatabaseError(f"DB connection failed: {e}") from e
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
        except:
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
        except (Full,sqlite3.Error):
            try: conn.close()
            except: pass
    def close_all(self):
        self._closed=True
        with self._lock:
            while not self._connections.empty():
                try: c=self._connections.get_nowait(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()
                except: break
            self._in_use.clear()

class DatabaseMetrics:
    def __init__(self):
        self._lock=threading.RLock(); self._q=0; self._e=0; self._t=0.0; self._slow=1.0
    def record_success(self,d): 
        with self._lock: self._q+=1; self._t+=d
        if d>self._slow: logger.warning(f"Slow query: {d:.3f}s")
    def record_error(self,d): 
        with self._lock: self._e+=1; self._t+=d
    def get_summary(self):
        with self._lock:
            avg=self._t/self._q if self._q else 0; er=self._e/self._q*100 if self._q else 0
            return {"total_queries":self._q,"error_count":self._e,"error_rate_pct":er,"avg_query_time_s":avg,"total_query_time_s":self._t}

class ThreadSafeDatabase:
    def __init__(self, db_path=DATABASE_FILE):
        self.db_path=db_path; self.connection_pool=DatabaseConnectionPool(db_path, max_connections=3)
        self._qlock=threading.RLock(); self._metrics=DatabaseMetrics(); self._vcache={}; self._vlock=threading.RLock()
        self._ensure()

    def _ensure(self):
        logger.info("DB integrity check")
        if not self._verify(): self._init_new(); return
        if not self._integrity_check()["healthy"]: self._handle_corruption()
        self._migrate()

    def _verify(self):
        if not os.path.exists(self.db_path): return False
        if os.stat(self.db_path).st_size==0: return False
        try:
            with open(self.db_path,'rb') as f:
                if not f.read(16).startswith(b'SQLite format 3'): return False
        except: return False
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
        except sqlite3.Error as e: r["errors"].append(str(e))
        return r

    def _handle_corruption(self):
        bp=self._create_emergency_backup("corruption")
        try:
            with self.connection_pool.get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"); conn.execute("VACUUM")
        except: pass
        if not self._restore_backup():
            if bp and self._restore_specific(bp): return
            self._init_new()

    def _create_emergency_backup(self, reason):
        try:
            ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S%f"); d=Path("backups/emergency"); d.mkdir(parents=True,exist_ok=True)
            bp=d/f"emergency_{reason}_{ts}.db"
            with self.connection_pool.get_connection() as conn: conn.execute(f"VACUUM INTO '{bp}'")
            with sqlite3.connect(str(bp)) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0]!="ok": bp.unlink(missing_ok=True); return None
            return str(bp)
        except: return None

    def _migrate(self):
        try:
            with self.connection_pool.get_connection() as conn:
                cv=conn.execute("PRAGMA user_version").fetchone()[0]
                needed=[(v,sql) for v,sql in sorted(self._migrations().items()) if v>cv]
                if not needed: return
            bp=self._create_emergency_backup("pre_migration")
            if not bp: return
            with self.connection_pool.get_connection() as conn:
                for v,sql in needed: conn.executescript(sql); conn.execute(f"PRAGMA user_version={v}")
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            if 'bp' in dir() and bp: self._restore_specific(bp)

    def _migrations(self):
        return {
            1: """
CREATE TABLE IF NOT EXISTS vessels (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,vessel_type TEXT,engine_make TEXT,engine_model TEXT,current_location TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS inventory_items (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,part_number TEXT,category TEXT,current_stock INTEGER DEFAULT 0,minimum_stock INTEGER DEFAULT 0,unit_cost REAL,supplier_id INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(supplier_id) REFERENCES suppliers(id));
CREATE TABLE IF NOT EXISTS maintenance_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,vessel_id INTEGER NOT NULL,system TEXT,task_name TEXT NOT NULL,description TEXT,next_due TEXT,status TEXT DEFAULT 'pending',priority TEXT DEFAULT 'medium',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(vessel_id) REFERENCES vessels(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,contact TEXT,phone TEXT,email TEXT,preferred INTEGER DEFAULT 0,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
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
        }

    def _init_new(self):
        self.connection_pool.close_all()
        if os.path.exists(self.db_path): os.remove(self.db_path)
        self.connection_pool=DatabaseConnectionPool(self.db_path,max_connections=3)
        with self.connection_pool.get_connection() as conn:
            for v,sql in sorted(self._migrations().items()): conn.executescript(sql); conn.execute(f"PRAGMA user_version={v}")

    def _restore_backup(self):
        try:
            backups=sorted(Path("backups").rglob("*.db"),key=lambda p:p.stat().st_mtime,reverse=True)
            for c in backups:
                try:
                    with sqlite3.connect(str(c)) as t:
                        if t.execute("PRAGMA quick_check").fetchone()[0]=="ok":
                            self.connection_pool.close_all(); shutil.copy2(str(c),self.db_path)
                            self.connection_pool=DatabaseConnectionPool(self.db_path,max_connections=3); return True
                except: continue
        except: pass
        return False

    def _restore_specific(self, p):
        try: self.connection_pool.close_all(); shutil.copy2(p,self.db_path); self.connection_pool=DatabaseConnectionPool(self.db_path,max_connections=3); return True
        except: return False

    @contextmanager
    def cursor(self):
        st=time.time()
        with self._qlock,self.connection_pool.get_connection() as conn:
            c=conn.cursor()
            try: yield c; self._metrics.record_success(time.time()-st)
            except sqlite3.Error as e: self._metrics.record_error(time.time()-st); raise
            except: self._metrics.record_error(time.time()-st); raise

    def q(self, query, params=()):
        with self.cursor() as c: c.execute(query,params); return [dict(r) for r in c.fetchall()]
    def u(self, query, params=()):
        with self.cursor() as c: c.execute(query,params); return c.rowcount
    def ins(self, query, params=()):
        with self.cursor() as c: c.execute(query,params); return c.lastrowid

    # ---- Vessels ----
    def get_vessels(self):
        r=self.q("SELECT * FROM vessels ORDER BY name")
        with self._vlock: self._vcache={row['id']:row['name'] for row in r}
        return r
    def get_vessel(self, vid):
        r=self.q("SELECT * FROM vessels WHERE id=?",(vid,)); return r[0] if r else None
    def add_vessel(self,v):
        DataValidator.validate_vessel(v)
        rid=self.ins("INSERT INTO vessels(name,vessel_type,engine_make,engine_model,current_location) VALUES(?,?,?,?,?)",
            (v.name.strip(),v.vessel_type.strip(),v.engine_make.strip(),v.engine_model.strip(),v.current_location.strip()))
        with self._vlock: self._vcache[rid]=v.name.strip()
        return rid
    def update_vessel(self,v):
        DataValidator.validate_vessel(v)
        rc=self.u("UPDATE vessels SET name=?,vessel_type=?,engine_make=?,engine_model=?,current_location=? WHERE id=?",
            (v.name.strip(),v.vessel_type.strip(),v.engine_make.strip(),v.engine_model.strip(),v.current_location.strip(),v.id))
        with self._vlock: self._vcache[v.id]=v.name.strip()
        return rc
    def delete_vessel(self,vid):
        rc=self.u("DELETE FROM vessels WHERE id=?",(vid,))
        with self._vlock: self._vcache.pop(vid,None)
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
        return self.ins("INSERT INTO maintenance_tasks(vessel_id,system,task_name,description,next_due,status,priority) VALUES(?,?,?,?,?,?,?)",
            (t.vessel_id,t.system.strip(),t.task_name.strip(),t.description.strip(),t.next_due.strip(),t.status,t.priority))
    def update_task(self, t):
        DataValidator.validate_task(t)
        return self.u("UPDATE maintenance_tasks SET vessel_id=?,system=?,task_name=?,description=?,next_due=?,status=?,priority=? WHERE id=?",
            (t.vessel_id,t.system.strip(),t.task_name.strip(),t.description.strip(),t.next_due.strip(),t.status,t.priority,t.id))
    def delete_task(self,tid): return self.u("DELETE FROM maintenance_tasks WHERE id=?",(tid,))
    def task_counts(self):
        r=self.q("SELECT status,COUNT(*) c FROM maintenance_tasks GROUP BY status"); counts={row['status']:row['c'] for row in r}
        due=self.q("SELECT COUNT(*) c FROM maintenance_tasks WHERE status='pending' AND next_due IS NOT NULL AND next_due<=date('now','+7 days')")
        return {'total':sum(counts.values()),'pending':counts.get('pending',0),'in_progress':counts.get('in_progress',0),'completed':counts.get('completed',0),'overdue':counts.get('overdue',0),'due_soon':due[0]['c'] if due else 0}

    # ---- Inventory ----
    def get_items(self): return self.q("SELECT * FROM inventory_items ORDER BY name")
    def get_item(self, iid):
        r=self.q("SELECT * FROM inventory_items WHERE id=?",(iid,)); return r[0] if r else None
    def add_item(self,name,pn,cat,stock,minstock,cost):
        name=DataValidator.validate_item_name(name); DataValidator.validate_stock(stock,minstock)
        return self.ins("INSERT INTO inventory_items(name,part_number,category,current_stock,minimum_stock,unit_cost) VALUES(?,?,?,?,?,?)",
            (name,pn.strip(),cat.strip(),stock,minstock,cost))
    def update_item(self,iid,name,pn,cat,stock,minstock,cost):
        name=DataValidator.validate_item_name(name); DataValidator.validate_stock(stock,minstock)
        return self.u("UPDATE inventory_items SET name=?,part_number=?,category=?,current_stock=?,minimum_stock=?,unit_cost=? WHERE id=?",
            (name,pn.strip(),cat.strip(),stock,minstock,cost,iid))
    def delete_item(self,iid): return self.u("DELETE FROM inventory_items WHERE id=?",(iid,))
    def adjust_stock(self,iid,delta,notes=""):
        item=self.get_item(iid)
        if not item: raise ValidationError("Item not found")
        new_stock=item['current_stock']+delta
        if new_stock<0: raise ValidationError(f"Insufficient stock (have {item['current_stock']}, need {-delta})")
        self.u("UPDATE inventory_items SET current_stock=? WHERE id=?",(new_stock,iid))
        tx_type="add" if delta>0 else "remove"
        self.ins("INSERT INTO stock_transactions(item_id,quantity,transaction_type,notes) VALUES(?,?,?,?)",(iid,abs(delta),tx_type,notes.strip()))
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
        return self.ins("INSERT INTO trip_logs(vessel_id,departure,arrival,destination,distance,fuel_used) VALUES(?,?,?,?,?,?)",
            (t.vessel_id,t.departure.strip(),t.arrival.strip(),t.destination.strip(),t.distance,t.fuel_used))
    def update_trip(self,t):
        if t.vessel_id<=0: raise ValidationError("Select a vessel")
        return self.u("UPDATE trip_logs SET vessel_id=?,departure=?,arrival=?,destination=?,distance=?,fuel_used=? WHERE id=?",
            (t.vessel_id,t.departure.strip(),t.arrival.strip(),t.destination.strip(),t.distance,t.fuel_used,t.id))
    def delete_trip(self,tid): return self.u("DELETE FROM trip_logs WHERE id=?",(tid,))

    # ---- Suppliers ----
    def get_suppliers(self): return self.q("SELECT * FROM suppliers ORDER BY name")
    def add_supplier(self,s):
        DataValidator.validate_supplier(s)
        return self.ins("INSERT INTO suppliers(name,contact,phone,email,preferred) VALUES(?,?,?,?,?)",
            (s.name.strip(),s.contact.strip(),s.phone.strip(),s.email.strip(),1 if s.preferred else 0))
    def update_supplier(self,s):
        DataValidator.validate_supplier(s)
        return self.u("UPDATE suppliers SET name=?,contact=?,phone=?,email=?,preferred=? WHERE id=?",
            (s.name.strip(),s.contact.strip(),s.phone.strip(),s.email.strip(),1 if s.preferred else 0,s.id))
    def delete_supplier(self,sid): return self.u("DELETE FROM suppliers WHERE id=?",(sid,))

    # ---- Stock Transactions ----
    def get_transactions(self,iid=None):
        if iid: rows=self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id WHERE st.item_id=? ORDER BY st.created_at DESC",(iid,))
        else: rows=self.q("SELECT st.*,ii.name item_name FROM stock_transactions st JOIN inventory_items ii ON st.item_id=ii.id ORDER BY st.created_at DESC")
        return rows

    # ---- Backup ----
    def create_backup(self,path):
        try:
            with self.connection_pool.get_connection() as conn: conn.execute(f"VACUUM INTO '{path}'")
            with sqlite3.connect(path) as v:
                if v.execute("PRAGMA integrity_check").fetchone()[0]=="ok": return True
                os.remove(path); return False
        except: return False

    # ---- Export ----
    def export_csv(self,path,query,params=()):
        rows=self.q(query,params)
        if not rows: return 0
        with open(path,'w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
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
            [("vessel_id","Vessel *",'combo'),("system","System",str),("task_name","Task Name *",str),("description","Description",'textview'),("next_due","Due (YYYY-MM-DD)",str),("status","Status",'combo'),("priority","Priority",'combo')])
        vessels=db.get_vessels(); self._vmap={v['name']:v['id'] for v in vessels}; self._rvmap={v['id']:v['name'] for v in vessels}
        self.set_combo("vessel_id",list(self._vmap.keys())); self.set_combo("status",["pending","in_progress","completed","overdue"]); self.set_combo("priority",["low","medium","high","critical"])
        self._tid=task.id if task else None
        if task:
            self.set_combo("vessel_id",list(self._vmap.keys()),self._rvmap.get(task.vessel_id,''))
            self.set_val("system",task.system); self.set_val("task_name",task.task_name); self.set_val("description",task.description)
            self.set_val("next_due",task.next_due)
            self.set_combo("status",["pending","in_progress","completed","overdue"],task.status)
            self.set_combo("priority",["low","medium","high","critical"],task.priority)
    def get_task(self):
        vn=self.get_val("vessel_id")
        return MaintenanceTask(id=self._tid,vessel_id=self._vmap.get(vn,0),vessel_name=vn,system=self.get_val("system"),
            task_name=self.get_val("task_name"),description=self.get_val("description"),next_due=self.get_val("next_due"),
            status=self.get_val("status"),priority=self.get_val("priority"))

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

class PreferencesDialog(Gtk.Dialog):
    def __init__(self,parent,config):
        super().__init__(title="Preferences",transient_for=parent,flags=0,modal=True)
        self.set_default_size(400,300); self.add_button("Cancel",Gtk.ResponseType.CANCEL); self.add_button("Save",Gtk.ResponseType.OK)
        self._config=config
        box=self.get_content_area(); box.set_spacing(8); box.set_border_width(12)
        notebook=Gtk.Notebook()
        gen=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8); gen.set_border_width(12)
        self._entries={}
        for key,label,default,typ in [
            ("due_soon_days","Due-soon threshold (days)",str(config.get("due_soon_days",7)),str),
            ("backup_retention_days","Backup retention (days)",str(config.get("backup_retention_days",30)),str),
            ("backup_interval_hours","Backup interval (hours)",str(config.get("backup_interval_hours",24)),str),
            ("auto_backup","Auto backup",["No","Yes"][config.get("auto_backup",True)],'combo'),
        ]:
            row=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=8)
            lbl=Gtk.Label(label=label,xalign=0); lbl.set_size_request(180,-1); row.pack_start(lbl,False,False,0)
            if typ==str: w=Gtk.Entry(); w.set_text(default); w.set_width_chars(15)
            elif typ=='combo':
                w=Gtk.ComboBoxText()
                for val in ["No","Yes"]: w.append(val,val)
                w.set_active(0 if default=="No" else 1)
            row.pack_start(w,False,False,0); gen.pack_start(row,False,False,0)
            self._entries[key]=w
        notebook.append_page(gen,Gtk.Label(label="General"))
        btn_box=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=8)
        btn_box.set_border_width(12)
        self._backup_btn=Gtk.Button(label="Show Backup Location")
        self._backup_btn.connect("clicked",lambda x: self._show_backup_info())
        btn_box.pack_start(self._backup_btn,False,False,0)
        notebook.append_page(btn_box,Gtk.Label(label="Storage"))
        box.pack_start(notebook,True,True,0)
        self.show_all()
    def _show_backup_info(self):
        d=Gtk.MessageDialog(transient_for=self,flags=0,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.OK,text="Backup Information")
        d.format_secondary_text(f"Backups stored in:\n{os.path.abspath('backups')}\n\nEmergency backups in:\n{os.path.abspath('backups/emergency')}")
        d.run(); d.destroy()
    def apply(self):
        try:
            self._config.set("due_soon_days",max(1,int(self._entries["due_soon_days"].get_text())))
            self._config.set("backup_retention_days",max(1,int(self._entries["backup_retention_days"].get_text())))
            self._config.set("backup_interval_hours",max(1,int(self._entries["backup_interval_hours"].get_text())))
            self._config.set("auto_backup",self._entries["auto_backup"].get_active_text()=="Yes")
            self._config.save()
        except ValueError as e: show_error(self,f"Invalid number: {e}")

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
        try: self._actions.put((fn,a,kw),timeout=0.1)
        except Full:
            self._over+=1
            if self._over%100==0: logger.warning(f"Queue overflow: {self._over} dropped")
            return
        with self._lock:
            if not self._proc: self._proc=True; GLib.idle_add(self._process)
    def _process(self):
        if self._shutdown: return False
        try:
            while True:
                fn,a,kw=self._actions.get_nowait()
                try: fn(*a,**kw)
                except Exception as e: logger.error(f"UI action failed: {e}")
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
            except Exception as e: logger.error(f"Refresh failed: {e}")
            finally:
                with self._lock:
                    self._refreshing=False; self._last=now
                    if self._pending: self._pending=False; GLib.timeout_add(100,self.schedule)
        threading.Thread(target=_run,daemon=True).start()
    def _do(self):
        app=self.app
        if app._shutting: return
        v=app.db.get_vessels(); inv=app.db.get_items(); tasks=app.db.get_tasks_with_vessels()
        isum=app.db.inventory_summary(); tc=app.db.task_counts(); vc=app.db.vessel_count()
        trips=app.db.get_trips(); suppliers=app.db.get_suppliers()
        app.ui.queue(app._update_views,v,inv,tasks,isum,tc,vc,trips,suppliers)

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
        self.set_default_size(self.config.get("window_width",1200),self.config.get("window_height",800))
        self._refresh_mgr=RefreshManager(self); self._sb=None; self._setup_ui(); self._apply_css(); self._restore_pos(); self._start_bg(); GLib.idle_add(self.refresh)
        self._setup_accels()
        logger.info("Application initialized")

    def _setup_accels(self):
        accel_group = Gtk.AccelGroup()
        accel_group.connect(Gdk.keyval_from_name("F5"), 0, Gtk.AccelFlags.VISIBLE, lambda *_: self.refresh())
        accel_group.connect(Gdk.keyval_from_name("Delete"), 0, Gtk.AccelFlags.VISIBLE, lambda *_: self._on_delete_current())
        self.add_accel_group(accel_group)

    def _on_delete_current(self):
        page=self.notebook.get_current_page()
        if page==1: self._del_vessel()
        elif page==2: self._del_inventory()
        elif page==3: self._del_task()
        elif page==4: self._del_trip()
        elif page==5: self._del_supplier()

    def _setup_menu_actions(self):
        group=Gio.SimpleActionGroup()
        actions=[
            ("export",lambda: self._export_dialog()),
            ("backup",lambda: self._manual_backup()),
            ("prefs",lambda: self._show_prefs()),
            ("about",lambda: AboutDialog(self)),
            ("quit",lambda: self._shutdown()),
        ]
        for name,cb in actions:
            act=Gio.SimpleAction.new(name,None)
            act.connect("activate",lambda a,p,cb=cb:cb())
            group.add_action(act)
        self.insert_action_group("app",group)
        m=Gio.Menu()
        fs=Gio.Menu(); fs.append("Export to CSV","app.export"); fs.append("Backup Database","app.backup"); fs.append("Preferences","app.prefs"); m.append_section(None,fs)
        hs=Gio.Menu(); hs.append("About","app.about"); hs.append("Quit","app.quit"); m.append_section(None,hs)
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
        d=PreferencesDialog(self,self.config)
        if d.run()==Gtk.ResponseType.OK: d.apply()
        d.destroy()

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
            self.notebook.append_page(self._supplier_tab(),Gtk.Label(label="  Suppliers  "))
            mv.pack_start(self.notebook,True,True,0)

            self._sb=Gtk.Statusbar(); self._sbc=self._sb.get_context_id("main"); mv.pack_end(self._sb,False,False,0)
            self._status("Ready")
        except Exception as e:
            logger.error(f"UI setup: {e}"); raise ResourceError(f"UI failed: {e}")

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

    def _edit_current(self,w=None):
        p=self.notebook.get_current_page()
        if p==1: self._edit_vessel()
        elif p==2: self._edit_inventory()
        elif p==3: self._edit_task()
        elif p==4: self._edit_trip()
        elif p==5: self._edit_supplier()

    def _delete_current(self,w=None):
        self._on_delete_current()

    # ---- DASHBOARD ----
    def _dash_tab(self):
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12); b.set_border_width(12)
        h=Gtk.Label(label="Dashboard — Overview & Quick Actions"); h.get_style_context().add_class("professional-header"); b.pack_start(h,False,False,0)

        # Empty-state welcome panel (hidden when data exists)
        self._welcome_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        self._welcome_box.set_border_width(20)
        wel=Gtk.Label()
        wel.set_markup("<big><b>Welcome to Vessel Keeper</b></big>")
        self._welcome_box.pack_start(wel,False,False,0)
        sub=Gtk.Label(label="Get started by adding vessels, inventory items, or maintenance tasks.")
        self._welcome_box.pack_start(sub,False,False,0)
        sample_btn=Gtk.Button(label="Load Sample Data"); sample_btn.set_image(Gtk.Image.new_from_icon_name("document-open-recent-symbolic",Gtk.IconSize.BUTTON)); sample_btn.set_always_show_image(True)
        sample_btn.get_style_context().add_class("suggested-action")
        sample_btn.connect("clicked",lambda x:self._seed_sample_data())
        self._welcome_box.pack_start(sample_btn,False,False,0)
        b.pack_start(self._welcome_box,False,False,0)

        # Summary cards
        cb=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=8); cb.set_homogeneous(True)
        self._cd_v=SummaryCard("Vessels","—","Total registered","#667eea")
        self._cd_t=SummaryCard("Tasks Due","—","Next 7 days","#e67e22")
        self._cd_s=SummaryCard("Low Stock","—","Items below minimum","#e74c3c")
        self._cd_o=SummaryCard("Overdue","—","Past-due tasks","#c0392b")
        self._cd_vl=SummaryCard("Inventory Value","$0","Total stock value","#27ae60")
        for c in (self._cd_v,self._cd_t,self._cd_s,self._cd_o,self._cd_vl): cb.pack_start(c,True,True,0)
        b.pack_start(cb,False,False,0)

        af=Gtk.Frame(label="Quick Actions — Click a button to add data")
        ab=Gtk.FlowBox(); ab.set_border_width(12); ab.set_max_children_per_line(6); ab.set_selection_mode(Gtk.SelectionMode.NONE)
        for lbl,icon,cb2 in [
            ("Add Vessel","list-add-symbolic",self._add_vessel),
            ("New Task","appointment-new-symbolic",self._add_task),
            ("Add Item","insert-object-symbolic",self._add_inventory),
            ("New Trip","media-record-symbolic",self._add_trip),
            ("Add Supplier","contact-new-symbolic",self._add_supplier),
            ("Refresh","view-refresh-symbolic",self.refresh),
        ]:
            btn=Gtk.Button(label=lbl); btn.set_image(Gtk.Image.new_from_icon_name(icon,Gtk.IconSize.BUTTON)); btn.set_always_show_image(True); btn.connect("clicked",lambda x,cb2=cb2:cb2())
            ab.add(btn)
        af.add(ab); b.pack_start(af,False,False,0)

        # Recent activity
        rf=Gtk.Frame(label="Recent Activity")
        self._rv=Gtk.ListStore(str,str,str)
        rv=Gtk.TreeView(model=self._rv)
        rv.set_vexpand(False)
        for i,n in [(0,"Time"),(1,"Entity"),(2,"Action")]:
            c=Gtk.TreeViewColumn(n,Gtk.CellRendererText(),text=i); rv.append_column(c)
        sw=Gtk.ScrolledWindow(); sw.set_min_content_height(120); sw.add(rv); rf.add(sw)
        b.pack_start(rf,True,True,0)

        # Helpful tips
        tip=Gtk.Label()
        tip.set_markup("\n<i>Tip: Use the tabs above to manage each area. Click <b>Add</b> in any tab toolbar to create new entries.</i>")
        b.pack_start(tip,False,False,0)
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
            show_error(self,f"Failed to load sample data: {e}")

    # ---- VESSELS ----
    def _vessel_tab(self):
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); b.set_border_width(6)
        tb,se=self._make_toolbar([
            ("Add","list-add-symbolic",self._add_vessel),("Edit","document-edit-symbolic",self._edit_vessel),
            ("Delete","edit-delete-symbolic",self._del_vessel),("Export","document-save-as-symbolic",lambda:self._export("vessels")),
        ], search_cb=lambda t:self._filter_vessels(t))
        self._vessel_search=se; b.pack_start(tb,False,False,0)

        self._vs=Gtk.ListStore(int,str,str,str,str,str)
        tv=self._treeview(self._vs)
        for i,n in [(1,"Name"),(2,"Type"),(3,"Engine Make"),(4,"Engine Model"),(5,"Location")]:
            c=Gtk.TreeViewColumn(n,Gtk.CellRendererText(),text=i); c.set_resizable(True); c.set_sort_column_id(i); tv.append_column(c)
        tv.connect("row-activated",lambda *_:self._edit_vessel()); sw=Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.add(tv); b.pack_start(sw,True,True,0)
        self._vtv=tv
        return b

    def _filter_vessels(self,t):
        self._vs.clear()
        data=getattr(self,'_all_vessels',[])
        for v in data:
            if not t or any(t in str(v.get(k,'')).lower() for k in ('name','vessel_type','engine_make','engine_model','current_location')):
                self._vs.append([v.get('id',0),v.get('name',''),v.get('vessel_type',''),v.get('engine_make',''),v.get('engine_model',''),v.get('current_location','')])

    # ---- INVENTORY ----
    def _inventory_tab(self):
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); b.set_border_width(6)
        tb,se=self._make_toolbar([
            ("Add","list-add-symbolic",self._add_inventory),("Edit","document-edit-symbolic",self._edit_inventory),
            ("Delete","edit-delete-symbolic",self._del_inventory),("Adjust Stock","document-properties-symbolic",self._adjust_stock),
            ("Export","document-save-as-symbolic",lambda:self._export("inventory")),
        ], search_cb=lambda t:self._filter_items(t))
        self._inv_search=se; b.pack_start(tb,False,False,0)

        self._is=Gtk.ListStore(int,str,str,str,int,int,float)
        tv=self._treeview(self._is)
        for i,n in [(1,"Name"),(2,"Part #"),(3,"Category"),(4,"Stock"),(5,"Min"),(6,"Unit Cost")]:
            r=Gtk.CellRendererText()
            if i in (4,5,6): r.set_alignment(1.0,0.5)
            c=Gtk.TreeViewColumn(n,r,text=i); c.set_resizable(True); c.set_sort_column_id(i)
            if i==6:
                def fmt(col,cell,model,itr,d):
                    v=model.get_value(itr,6); cell.set_property("text",f"${v:.2f}" if v else "$0.00")
                c=Gtk.TreeViewColumn("Unit Cost",Gtk.CellRendererText()); c.set_cell_data_func(Gtk.CellRendererText(),fmt)
            tv.append_column(c)
        def _scol(col,cell,model,itr,d):
            s=model.get_value(itr,4); m=model.get_value(itr,5)
            cell.set_property("cell-background","#fce4e4" if s<=m else None)
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r,Gtk.CellRendererText): c.set_cell_data_func(r,_scol)
        tv.connect("row-activated",lambda *_:self._edit_inventory()); sw=Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.add(tv); b.pack_start(sw,True,True,0)
        self._itv=tv
        return b

    def _filter_items(self,t):
        self._is.clear()
        for item in getattr(self,'_all_items',[]):
            if not t or any(t in str(item.get(k,'')).lower() for k in ('name','part_number','category')):
                self._is.append([item.get('id',0),item.get('name',''),item.get('part_number',''),item.get('category',''),item.get('current_stock',0),item.get('minimum_stock',0),item.get('unit_cost',0.0)])

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
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); b.set_border_width(6)
        tb,se=self._make_toolbar([
            ("Add","list-add-symbolic",self._add_task),("Edit","document-edit-symbolic",self._edit_task),
            ("Delete","edit-delete-symbolic",self._del_task),("Export","document-save-as-symbolic",lambda:self._export("tasks")),
        ], search_cb=lambda t:self._filter_tasks(t))
        self._task_search=se; b.pack_start(tb,False,False,0)

        self._ts=Gtk.ListStore(int,str,str,str,str,str,str)
        tv=self._treeview(self._ts)
        for i,n in [(1,"Vessel"),(2,"System"),(3,"Task"),(4,"Due"),(5,"Status"),(6,"Priority")]:
            c=Gtk.TreeViewColumn(n,Gtk.CellRendererText(),text=i); c.set_resizable(True); c.set_sort_column_id(i); tv.append_column(c)
        def _tcol(col,cell,model,itr,d):
            st=model.get_value(itr,5); pr=model.get_value(itr,6); bg=None
            if st=="overdue": bg="#fce4e4"
            elif st=="completed": bg="#e8f5e9"
            elif st=="in_progress": bg="#e3f2fd"
            if pr=="critical" and st!="completed": bg="#ffebee"
            cell.set_property("cell-background",bg)
        for c in tv.get_columns():
            for r in c.get_cells():
                if isinstance(r,Gtk.CellRendererText): c.set_cell_data_func(r,_tcol)
        tv.connect("row-activated",lambda *_:self._edit_task()); sw=Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.add(tv); b.pack_start(sw,True,True,0)
        self._ttv=tv
        return b

    def _filter_tasks(self,t):
        self._ts.clear()
        for task in getattr(self,'_all_tasks',[]):
            if not t or any(t in str(task.get(k,'')).lower() for k in ('vessel_name','system','task_name','status','priority')):
                self._ts.append([task.get('id',0),task.get('vessel_name',''),task.get('system',''),task.get('task_name',''),task.get('next_due',''),task.get('status',''),task.get('priority','')])

    def _filter_trips(self,t):
        self._trip_s.clear()
        for trip in getattr(self,'_all_trips',[]):
            if not t or any(t in str(trip.get(k,'')).lower() for k in ('vessel_name','destination','departure','arrival')):
                self._trip_s.append([trip['id'],trip.get('vessel_name',''),trip.get('destination',''),trip.get('departure',''),trip.get('arrival',''),trip.get('distance',0.0),trip.get('fuel_used',0.0)])

    def _filter_suppliers(self,t):
        self._ss.clear()
        for s in getattr(self,'_all_suppliers',[]):
            if not t or any(t in str(s.get(k,'')).lower() for k in ('name','contact','phone','email')):
                self._ss.append([s['id'],s['name'],s.get('contact',''),s.get('phone',''),s.get('email',''),"Yes" if s.get('preferred') else "No"])

    # ---- TRIPS ----
    def _trip_tab(self):
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); b.set_border_width(6)
        tb,se=self._make_toolbar([
            ("Add","list-add-symbolic",self._add_trip),("Edit","document-edit-symbolic",self._edit_trip),
            ("Delete","edit-delete-symbolic",self._del_trip),("Export","document-save-as-symbolic",lambda:self._export("trips")),
        ], search_cb=lambda t:self._filter_trips(t))
        self._trip_search=se; b.pack_start(tb,False,False,0)

        self._trip_s=Gtk.ListStore(int,str,str,str,str,float,float)
        tv=self._treeview(self._trip_s)
        for i,n in [(1,"Vessel"),(2,"Destination"),(3,"Departure"),(4,"Arrival"),(5,"Distance (nm)"),(6,"Fuel Used")]:
            r=Gtk.CellRendererText()
            if i in (5,6): r.set_alignment(1.0,0.5)
            c=Gtk.TreeViewColumn(n,r,text=i); c.set_resizable(True); c.set_sort_column_id(i); tv.append_column(c)
        tv.connect("row-activated",lambda *_:self._edit_trip()); sw=Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.add(tv); b.pack_start(sw,True,True,0)
        self._trip_tv=tv
        return b

    # ---- SUPPLIERS ----
    def _supplier_tab(self):
        b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); b.set_border_width(6)
        tb,se=self._make_toolbar([
            ("Add","list-add-symbolic",self._add_supplier),("Edit","document-edit-symbolic",self._edit_supplier),
            ("Delete","edit-delete-symbolic",self._del_supplier),("Export","document-save-as-symbolic",lambda:self._export("suppliers")),
        ], search_cb=lambda t:self._filter_suppliers(t))
        self._supplier_search=se; b.pack_start(tb,False,False,0)

        self._ss=Gtk.ListStore(int,str,str,str,str,str)
        tv=self._treeview(self._ss)
        for i,n in [(1,"Name"),(2,"Contact"),(3,"Phone"),(4,"Email"),(5,"Preferred")]:
            c=Gtk.TreeViewColumn(n,Gtk.CellRendererText(),text=i); c.set_resizable(True); c.set_sort_column_id(i); tv.append_column(c)
        tv.connect("row-activated",lambda *_:self._edit_supplier()); sw=Gtk.ScrolledWindow(); sw.set_vexpand(True); sw.add(tv); b.pack_start(sw,True,True,0)
        self._stv=tv
        return b

    # ========== CSS ==========
    def _apply_css(self):
        css="""
        .professional-header { background-color:#667eea; color:white; font-weight:bold; padding:10px; border-radius:5px; font-size:16px; }
        .card-title { font-size:11px; color:rgba(255,255,255,0.85); }
        .card-value { font-size:28px; font-weight:bold; color:white; }
        .card-subtitle { font-size:10px; color:rgba(255,255,255,0.7); }
        GtkTreeView { font-size:13px; }
        GtkNotebook tab { padding:6px 12px; }
        GtkStatusbar { border-top:1px solid #ccc; }
        """
        p=Gtk.CssProvider()
        try: p.load_from_data(css.encode()); Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(),p,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception as e: logger.warning(f"CSS failed: {e}")

    def _restore_pos(self):
        try: self.move(self.config.get("window_x",100),self.config.get("window_y",100))
        except: pass

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
            try: t=d.get_task(); self.db.add_task(t); self.refresh(); self._status(f"Task added")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _edit_task(self):
        sel=self._ttv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        tid=self._ts.get_value(ti,0); task=self.db.get_task(tid)
        if not task: return
        d=MaintenanceDialog(self,self.db,MaintenanceTask.from_dict(task))
        if d.run()==Gtk.ResponseType.OK:
            try: t=d.get_task(); t.id=tid; self.db.update_task(t); self.refresh(); self._status(f"Task updated")
            except (ValidationError,DatabaseError) as e: show_error(self,str(e))
        d.destroy()
    def _del_task(self):
        sel=self._ttv.get_selection(); _,ti=sel.get_selected()
        if not ti: return
        name=self._ts.get_value(ti,3); tid=self._ts.get_value(ti,0)
        if confirm(self,f"Delete task '{name}'?"):
            try: self.db.delete_task(tid); self.refresh(); self._status(f"Task deleted")
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

    def _update_views(self, vessels, items, tasks, inv_summary, task_counts, vessel_count, trips, suppliers):
        request_id.set(str(uuid.uuid4())[:8])
        self._all_vessels=vessels; self._all_items=items; self._all_tasks=tasks; self._all_trips=trips; self._all_suppliers=suppliers
        has_data = vessel_count > 0 or task_counts.get('total', 0) > 0 or inv_summary.get('total', 0) > 0

        # Show/hide welcome panel based on data presence
        if hasattr(self, '_welcome_box'):
            self._welcome_box.set_visible(not has_data)

        # Dashboard
        self._cd_v.update(str(vessel_count))
        self._cd_t.update(str(task_counts.get('due_soon',0)))
        self._cd_s.update(str(inv_summary.get('low_count',0)))
        self._cd_o.update(str(task_counts.get('overdue',0)))
        inv_val=inv_summary.get('total_value',0)
        self._cd_vl.update(f"${inv_val:,.0f}")

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
            if not st or any(st in str(task.get(k,'')).lower() for k in('vessel_name','system','task_name','status','priority')):
                self._ts.append([task['id'],task.get('vessel_name',''),task.get('system',''),task['task_name'],task.get('next_due',''),task.get('status',''),task.get('priority','')])

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

        # Recent activity
        self._rv.clear()
        now=datetime.datetime.now().strftime("%H:%M")
        if vessel_count: self._rv.append([now,"Vessels",f"{vessel_count} vessels registered"])
        if task_counts.get('overdue',0): self._rv.append([now,"Tasks",f"{task_counts['overdue']} overdue tasks"])
        if inv_summary.get('low_count',0): self._rv.append([now,"Inventory",f"{inv_summary['low_count']} low-stock items"])

        self._status(f"{vessel_count} vessels | {task_counts.get('total',0)} tasks ({task_counts.get('overdue',0)} overdue) | {inv_summary.get('total',0)} items (${inv_val:,.0f}) | {len(trips)} trips | {len(suppliers)} suppliers")

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
            Path("backups").mkdir(exist_ok=True); ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S"); bp=f"backups/manual_{ts}.db"
            if self.db.create_backup(bp): self._status(f"Backup: {bp}")
            else: self._status("Backup failed")
        except Exception as e: show_error(self,f"Backup failed: {e}")

    # ========== BACKGROUND ==========
    def _start_bg(self):
        ev=threading.Event()
        def backup():
            while not self._shutting:
                try:
                    now=datetime.datetime.now()
                    next_run=now.replace(hour=2,minute=0,second=0)+datetime.timedelta(days=1)
                    wait=min((next_run-now).total_seconds(),3600)
                    if ev.wait(timeout=wait): break
                    if not self._shutting and self.config.get("auto_backup",True): self._auto_backup()
                except: ev.wait(300)
        threading.Thread(target=backup,daemon=True).start()
        def cleanup():
            while not self._shutting:
                if ev.wait(timeout=3600): break
                if not self._shutting: self._cleanup_backups()
        threading.Thread(target=cleanup,daemon=True).start()
        self._bg_event=ev

    def _auto_backup(self):
        try:
            Path("backups").mkdir(exist_ok=True); ts=datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            if self.db.create_backup(f"backups/vessel_keeper_backup_{ts}.db"): self._cleanup_backups()
        except Exception as e: logger.error(f"Auto backup failed: {e}")

    def _cleanup_backups(self):
        retention=self.config.get("backup_retention_days",30); cutoff=time.time()-retention*86400
        for f in Path("backups").glob("vessel_keeper_backup_*.db"):
            try:
                if f.stat().st_mtime<cutoff: f.unlink()
            except: pass
        for f in Path("backups/emergency").glob("emergency_*.db"):
            try:
                if f.stat().st_mtime<time.time()-7*86400: f.unlink()
            except: pass

    # ========== SHUTDOWN ==========
    def do_delete_event(self,e):
        if not self._shut_done: self._shutdown()
        return False
    def _shutdown(self):
        if self._shut_done: return
        self._shut_done=True; self._shutting=True; self.ui.request_shutdown()
        if hasattr(self,'_bg_event'): self._bg_event.set()
        try:
            x,y=self.get_position(); w,h=self.get_size()
            self.config.set("window_x",x); self.config.set("window_y",y)
            self.config.set("window_width",w); self.config.set("window_height",h)
            self.config.save()
        except: pass
        try: self.db.close()
        except: pass
        logger.info("Shutdown complete"); Gtk.main_quit()

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
        logger.critical("Uncaught",exc_info=(typ,val,tb)); show_error(None,f"Error: {val}")
    sys.excepthook=handler

def main():
    for d in [REPORTS_DIR,'backups','logs','backups/emergency']: Path(d).mkdir(exist_ok=True)
    setup_excepthook()
    app=None
    try:
        app=VesselKeeperApp(); app.connect("destroy",Gtk.main_quit); app.show_all()
        logger.info("Started"); Gtk.main()
    except Exception as e:
        logging.critical(f"Startup failed: {e}",exc_info=True); show_error(None,f"Failed: {e}"); sys.exit(1)
    finally:
        if app and not app._shut_done: app._shutdown()

if __name__=="__main__": main()

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
import contextvars
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Tuple, Callable, Iterator, TypedDict
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

# Context variables for distributed tracing
request_id = contextvars.ContextVar('request_id', default='system')

# =============================================================================
# TYPE DEFINITIONS
# =============================================================================
class VesselRow(TypedDict, total=False):
    """Type-safe vessel database row"""
    id: int
    name: str
    vessel_type: str
    engine_make: str
    engine_model: str
    current_location: str
    created_at: str

class MaintenanceTaskRow(TypedDict, total=False):
    """Type-safe maintenance task database row"""
    id: int
    vessel_id: int
    vessel_name: str
    system: str
    task_name: str
    description: str
    next_due: str
    status: str
    priority: str
    created_at: str

class InventoryItemRow(TypedDict, total=False):
    """Type-safe inventory item database row"""
    id: int
    name: str
    part_number: str
    category: str
    current_stock: int
    minimum_stock: int
    unit_cost: float
    supplier_id: int
    created_at: str

# =============================================================================
# ENHANCED LOGGING SETUP
# =============================================================================
class StructuredLogger:
    """Enterprise-grade structured logging with context and request tracing"""
    
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
        """Format context with request ID for tracing"""
        rid = request_id.get()
        base = f"request_id={rid}"
        extra = " | ".join(f"{k}={v}" for k, v in context.items())
        return f"{base} | {extra}" if extra else base

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
# VALIDATION LAYER
# =============================================================================
class DataValidator:
    """Centralized data validation with clear error messages"""
    
    @staticmethod
    def validate_vessel(vessel: Vessel) -> None:
        """Validate vessel data before database operations"""
        if not vessel.name or not vessel.name.strip():
            raise ValidationError("Vessel name cannot be empty")
        if len(vessel.name) > 255:
            raise ValidationError("Vessel name too long (maximum 255 characters)")
        if vessel.vessel_type and len(vessel.vessel_type) > 100:
            raise ValidationError("Vessel type too long (maximum 100 characters)")
        if vessel.engine_make and len(vessel.engine_make) > 100:
            raise ValidationError("Engine make too long (maximum 100 characters)")
        if vessel.engine_model and len(vessel.engine_model) > 100:
            raise ValidationError("Engine model too long (maximum 100 characters)")
    
    @staticmethod
    def validate_maintenance_task(task: MaintenanceTask) -> None:
        """Validate maintenance task data"""
        if task.vessel_id <= 0:
            raise ValidationError("Invalid vessel ID")
        if not task.task_name or not task.task_name.strip():
            raise ValidationError("Task name cannot be empty")
        if len(task.task_name) > 255:
            raise ValidationError("Task name too long (maximum 255 characters)")
        if task.status not in ['pending', 'in_progress', 'completed', 'overdue']:
            raise ValidationError(f"Invalid status: {task.status}")
        if task.priority not in ['low', 'medium', 'high', 'critical']:
            raise ValidationError(f"Invalid priority: {task.priority}")

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
    """Thread-safe database connection pool with health monitoring and improved backoff"""
    
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
        """Acquire connection with exponential backoff and health check"""
        deadline = time.time() + self.timeout
        retry_delay = 0.05  # Start with 50ms
        max_retry_delay = 1.0
        
        while time.time() < deadline:
            try:
                remaining_time = deadline - time.time()
                if remaining_time <= 0:
                    break
                
                timeout_for_get = min(1.0, remaining_time)
                conn = self._connections.get(timeout=timeout_for_get)
                
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
                # Exponential backoff with jitter
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, max_retry_delay)
        
        raise DatabaseError(f"Timeout acquiring database connection after {self.timeout}s")
    
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
    
    def get_pool_status(self) -> Dict[str, Any]:
        """Get connection pool health status"""
        with self._lock:
            return {
                "total_connections": self.max_connections,
                "in_use": len(self._in_use),
                "available": self._connections.qsize(),
                "pool_exhaustion_ratio": len(self._in_use) / self.max_connections
            }
    
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
        self._vessel_cache: Dict[int, str] = {}
        self._vessel_cache_lock = threading.RLock()
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
        """Create and verify emergency backup before relying on it"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = Path("backups/emergency")
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            backup_path = backup_dir / f"emergency_{reason}_{timestamp}.db"
            
            with self.connection_pool.get_connection() as conn:
                conn.execute(f"VACUUM INTO '{backup_path}'")
            
            # Verify backup integrity BEFORE returning
            try:
                with sqlite3.connect(str(backup_path)) as verify_conn:
                    result = verify_conn.execute("PRAGMA integrity_check").fetchone()
                    if result[0] != "ok":
                        logger.error(f"Backup verification failed: {result[0]}")
                        backup_path.unlink()
                        return None
            except sqlite3.Error as e:
                logger.error(f"Backup verification raised error: {e}")
                backup_path.unlink()
                return None
            
            logger.info(f"Emergency backup created and verified: {backup_path}")
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
        """Thread-safe cursor context manager with isolation levels.
        
        Example:
            with db.get_cursor() as cursor:
                cursor.execute("SELECT * FROM vessels")
                results = cursor.fetchall()
        
        Raises:
            DatabaseError: If connection acquisition or operation fails.
        """
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
    
    def get_vessels(self) -> List[VesselRow]:
        """Get all vessels with type safety."""
        result = self.execute_query("SELECT * FROM vessels ORDER BY name")
        # Update cache for N+1 query elimination
        with self._vessel_cache_lock:
            self._vessel_cache = {row['id']: row['name'] for row in result}
        return result
    
    def get_maintenance_tasks_with_vessels(self, use_cache: bool = True) -> List[MaintenanceTaskRow]:
        """Get maintenance tasks with vessel data, using cache to eliminate N+1 queries.
        
        Args:
            use_cache: Whether to use cached vessel lookups
        
        Returns:
            List of maintenance tasks with vessel names populated
        """
        # Ensure cache is populated
        if use_cache and not self._vessel_cache:
            self.get_vessels()
        
        tasks = self.execute_query("SELECT * FROM maintenance_tasks ORDER BY next_due")
        
        with self._vessel_cache_lock:
            for task in tasks:
                task['vessel_name'] = self._vessel_cache.get(task.get('vessel_id'), 'Unknown')
        
        return tasks
    
    def get_maintenance_tasks(self) -> List[MaintenanceTaskRow]:
        """Get all maintenance tasks with vessel names (legacy method)"""
        return self.get_maintenance_tasks_with_vessels(use_cache=True)
    
    def get_inventory_items(self) -> List[InventoryItemRow]:
        """Get all inventory items with type safety."""
        return self.execute_query("SELECT * FROM inventory_items ORDER BY name")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get database performance metrics"""
        return self._metrics.get_summary()
    
    def get_pool_status(self) -> Dict[str, Any]:
        """Get connection pool status"""
        return self.connection_pool.get_pool_status()
    
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
    """Thread-safe UI state management with action queuing and overflow protection"""
    
    def __init__(self, max_queue_size: int = 1000):
        self._ui_actions: Queue[Tuple[Callable, tuple, dict]] = Queue(maxsize=max_queue_size)
        self._is_processing = False
        self._action_lock = threading.RLock()
        self._queue_overflow_count = 0
    
    def queue_ui_action(self, action: Callable, *args, **kwargs):
        """Queue UI action for main thread execution with overflow protection"""
        try:
            self._ui_actions.put((action, args, kwargs), timeout=0.1)
        except Full:
            self._queue_overflow_count += 1
            if self._queue_overflow_count % 100 == 0:
                logger.warning(f"UI action queue overflow: {self._queue_overflow_count} dropped actions")
            return
        
        with self._action_lock:
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
    
    def get_vessels_summary(self, force_refresh: bool = False) -> List[VesselRow]:
        cache_key = 'vessels_summary'
        return self._get_cached_data(cache_key, self._load_vessels_summary, force_refresh)
    
    def get_inventory_summary(self, force_refresh: bool = False) -> List[InventoryItemRow]:
        cache_key = 'inventory_summary'
        return self._get_cached_data(cache_key, self._load_inventory_summary, force_refresh)
    
    def get_maintenance_tasks_summary(self, force_refresh: bool = False) -> List[MaintenanceTaskRow]:
        cache_key = 'maintenance_tasks_summary'
        return self._get_cached_data(cache_key, self._load_maintenance_tasks_summary, force_refresh)
    
    def _get_cached_data(self, cache_key: str, loader: Callable, force_refresh: bool) -> Any:
        """Get data from cache or load fresh with detailed TTL logging"""
        current_time = time.time()
        
        with self._load_lock:
            if not force_refresh and cache_key in self._cache:
                timestamp, data = self._cache[cache_key]
                elapsed = current_time - timestamp
                if elapsed < self._cache_ttl:
                    logger.debug(f"Cache hit for {cache_key} ({elapsed:.1f}s old)")
                    return data
                else:
                    logger.debug(f"Cache expired for {cache_key} ({elapsed:.1f}s > {self._cache_ttl}s)")
            
            try:
                data = loader()
                self._cache[cache_key] = (current_time, data)
                return data
            except Exception as e:
                logger.error(f"Failed to load data for {cache_key}: {e}")
                if cache_key in self._cache:
                    return self._cache[cache_key][1]
                raise
    
    def _load_vessels_summary(self) -> List[VesselRow]:
        return self.db.get_vessels()
    
    def _load_inventory_summary(self) -> List[InventoryItemRow]:
        return self.db.get_inventory_items()
    
    def _load_maintenance_tasks_summary(self) -> List[MaintenanceTaskRow]:
        return self.db.get_maintenance_tasks_with_vessels(use_cache=True)
    
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
    """Enterprise-grade main application with dependency injection support"""
    
    def __init__(self, db: Optional[ThreadSafeDatabase] = None, 
                 config: Optional[ConfigManager] = None):
        super().__init__(title="Vessel Keeper - Professional Edition")
        self.set_default_size(1200, 800)
        
        # Initialize core components with dependency injection for testing
        self.config_manager = config or ConfigManager()
        self.db = db or ThreadSafeDatabase()
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

def setup_global_exception_handler():
    """Setup global exception handling - separate concerns from main function"""
    def handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        show_error_dialog(None, f"Error: {exc_value}")
    
    sys.excepthook = handler

# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def main():
    """Professional main function with comprehensive error handling"""
    # Create necessary directories
    for directory in [REPORTS_DIR, 'backups', 'logs', 'backups/emergency']:
        Path(directory).mkdir(exist_ok=True)
    
    # Setup global exception handler
    setup_global_exception_handler()
    
    # Initialize and run application
    app = None
    try:
        app = VesselKeeperApp()
        app.connect("destroy", Gtk.main_quit)
        app.show_all()
        
        logger.info("Vessel Keeper started successfully")
        Gtk.main()
        
    except Exception as e:
        logging.critical(f"Failed to start Vessel Keeper: {e}", exc_info=True)
        show_error_dialog(None, f"Failed to start application: {e}")
        sys.exit(1)
    
    finally:
        if app and hasattr(app, '_shutdown_sequence'):
            app._shutdown_sequence()

if __name__ == "__main__":
    main()
