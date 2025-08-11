#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GObject, GLib
import sqlite3
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional, Dict
from enum import Enum, auto

# ======================
# ENUMS & CONSTANTS
# ======================

class Priority(Enum):
    CRITICAL = auto()
    HIGH = auto()
    MEDIUM = auto()
    LOW = auto()

class IntervalType(Enum):
    HOURS = auto()
    DAYS = auto()
    USAGE_CYCLES = auto()

VESSEL_TYPES = [
    "Motor Yacht", "Sailboat", "Fishing Vessel",
    "RIB", "Catamaran", "Trawler", "Other"
]

SYSTEM_CATEGORIES = [
    "Engine", "Electrical", "Hull", "Deck",
    "Navigation", "Plumbing", "Safety", "Other"
]

# ======================
# DATA MODELS
# ======================

@dataclass
class Vessel:
    id: Optional[int] = None
    name: str = ""
    registration: str = ""
    vessel_type: str = ""
    length: float = 0.0
    beam: float = 0.0
    draft: float = 0.0
    engine_model: str = ""
    engine_hours: float = 0.0
    hull_material: str = ""
    build_year: int = 0
    last_survey: str = ""
    insurance_expiry: str = ""
    current_location: str = ""
    notes: str = ""

@dataclass
class MaintenanceTask:
    id: Optional[int] = None
    vessel_id: int = 0
    vessel_name: str = ""
    system: str = ""
    task_name: str = ""
    template_id: Optional[int] = None
    interval_type: IntervalType = IntervalType.HOURS
    interval_value: int = 0
    last_completed: str = ""
    last_hours: float = 0.0
    next_due: str = ""
    priority: Priority = Priority.MEDIUM
    parts_required: List[int] = None
    instructions: str = ""

@dataclass
class MaintenanceLog:
    id: Optional[int] = None
    task_id: int = 0
    completion_date: str = ""
    engine_hours: float = 0.0
    technician: str = ""
    hours_worked: float = 0.0
    parts_used: List[int] = None
    cost: float = 0.0
    notes: str = ""

@dataclass
class InventoryItem:
    id: Optional[int] = None
    name: str = ""
    part_number: str = ""
    category: str = ""
    system: str = ""
    location: str = ""
    current_stock: int = 0
    minimum_stock: int = 0
    supplier: str = ""
    unit_cost: float = 0.0
    specifications: str = ""

@dataclass
class TripLog:
    id: Optional[int] = None
    vessel_id: int = 0
    departure: str = ""
    arrival: str = ""
    origin: str = ""
    destination: str = ""
    start_hours: float = 0.0
    end_hours: float = 0.0
    fuel_used: float = 0.0
    distance: float = 0.0
    crew: str = ""
    log_entries: str = ""

@dataclass
class MaintenanceTemplate:
    id: Optional[int] = None
    name: str = ""
    system: str = ""
    interval_hours: int = 0
    interval_days: int = 0
    instructions: str = ""
    parts_list: List[int] = None

# ======================
# DATABASE LAYER
# ======================

class Database:
    def __init__(self):
        self.db_path = os.path.expanduser("~/.local/share/vesselkeeper/data.db")
        self._create_data_dir()
        self.conn = sqlite3.connect(self.db_path)
        self._init_tables()
    
    def _create_data_dir(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _init_tables(self):
        cursor = self.conn.cursor()
        
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS vessels (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(name) <= 100),
            registration TEXT CHECK(length(registration) <= 50),
            vessel_type TEXT CHECK(vessel_type IN ('Motor Yacht','Sailboat','Fishing Vessel','RIB','Catamaran','Trawler','Other')),
            length REAL CHECK(length > 0),
            beam REAL CHECK(beam > 0),
            draft REAL CHECK(draft >= 0),
            engine_model TEXT,
            engine_hours REAL DEFAULT 0 CHECK(engine_hours >= 0),
            hull_material TEXT,
            build_year INTEGER CHECK(build_year > 1900),
            last_survey TEXT,
            insurance_expiry TEXT,
            current_location TEXT,
            notes TEXT
        );
        
        CREATE TABLE IF NOT EXISTS maintenance_tasks (
            id INTEGER PRIMARY KEY,
            vessel_id INTEGER REFERENCES vessels(id),
            system TEXT,
            task_name TEXT NOT NULL,
            template_id INTEGER,
            interval_type TEXT CHECK(interval_type IN ('HOURS','DAYS','USAGE_CYCLES')),
            interval_value INTEGER CHECK(interval_value > 0),
            last_completed TEXT,
            last_hours REAL CHECK(last_hours >= 0),
            next_due TEXT,
            priority TEXT CHECK(priority IN ('CRITICAL','HIGH','MEDIUM','LOW')),
            parts_required TEXT,
            instructions TEXT
        );
        
        CREATE TABLE IF NOT EXISTS maintenance_logs (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES maintenance_tasks(id),
            completion_date TEXT NOT NULL,
            engine_hours REAL CHECK(engine_hours >= 0),
            technician TEXT,
            hours_worked REAL CHECK(hours_worked >= 0),
            parts_used TEXT,
            cost REAL CHECK(cost >= 0),
            notes TEXT
        );
        
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(name) <= 100),
            part_number TEXT,
            category TEXT,
            system TEXT,
            location TEXT,
            current_stock INTEGER DEFAULT 0 CHECK(current_stock >= 0),
            minimum_stock INTEGER DEFAULT 0 CHECK(minimum_stock >= 0),
            supplier TEXT,
            unit_cost REAL DEFAULT 0 CHECK(unit_cost >= 0),
            specifications TEXT
        );
        
        CREATE TABLE IF NOT EXISTS trip_logs (
            id INTEGER PRIMARY KEY,
            vessel_id INTEGER REFERENCES vessels(id),
            departure TEXT NOT NULL,
            arrival TEXT,
            origin TEXT,
            destination TEXT,
            start_hours REAL CHECK(start_hours >= 0),
            end_hours REAL CHECK(end_hours >= 0),
            fuel_used REAL CHECK(fuel_used >= 0),
            distance REAL CHECK(distance >= 0),
            crew TEXT,
            log_entries TEXT
        );
        
        CREATE TABLE IF NOT EXISTS maintenance_templates (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            system TEXT,
            interval_hours INTEGER DEFAULT 0,
            interval_days INTEGER DEFAULT 0,
            instructions TEXT,
            parts_list TEXT
        );
        
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            contact TEXT,
            phone TEXT,
            email TEXT,
            notes TEXT
        );
        """)
        
        # Create indexes for performance
        cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_vessels_name ON vessels(name);
        CREATE INDEX IF NOT EXISTS idx_maintenance_vessel ON maintenance_tasks(vessel_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_partno ON inventory(part_number);
        CREATE INDEX IF NOT EXISTS idx_trips_vessel ON trip_logs(vessel_id);
        """)
        
        self.conn.commit()
    
    # Vessel operations
    def add_vessel(self, vessel: Vessel) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO vessels (
                name, registration, vessel_type, length, beam, draft,
                engine_model, engine_hours, hull_material, build_year,
                last_survey, insurance_expiry, current_location, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            vessel.name, vessel.registration, vessel.vessel_type,
            vessel.length, vessel.beam, vessel.draft,
            vessel.engine_model, vessel.engine_hours, vessel.hull_material,
            vessel.build_year, vessel.last_survey, vessel.insurance_expiry,
            vessel.current_location, vessel.notes
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_vessels(self) -> List[Vessel]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM vessels")
        return [Vessel(*row) for row in cursor.fetchall()]
    
    # Maintenance operations
    def add_maintenance_task(self, task: MaintenanceTask) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO maintenance_tasks (
                vessel_id, system, task_name, template_id, interval_type,
                interval_value, last_completed, last_hours, next_due,
                priority, parts_required, instructions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.vessel_id, task.system, task.task_name, task.template_id,
            task.interval_type.name, task.interval_value, task.last_completed,
            task.last_hours, task.next_due, task.priority.name,
            json.dumps(task.parts_required) if task.parts_required else None,
            task.instructions
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_maintenance_tasks(self, vessel_id: Optional[int] = None) -> List[MaintenanceTask]:
        cursor = self.conn.cursor()
        if vessel_id:
            cursor.execute("""
                SELECT mt.*, v.name 
                FROM maintenance_tasks mt
                JOIN vessels v ON mt.vessel_id = v.id
                WHERE mt.vessel_id = ?
            """, (vessel_id,))
        else:
            cursor.execute("""
                SELECT mt.*, v.name 
                FROM maintenance_tasks mt
                JOIN vessels v ON mt.vessel_id = v.id
            """)
        
        tasks = []
        for row in cursor.fetchall():
            tasks.append(MaintenanceTask(
                id=row[0],
                vessel_id=row[1],
                vessel_name=row[13],
                system=row[2],
                task_name=row[3],
                template_id=row[4],
                interval_type=IntervalType[row[5]],
                interval_value=row[6],
                last_completed=row[7],
                last_hours=row[8],
                next_due=row[9],
                priority=Priority[row[10]],
                parts_required=json.loads(row[11]) if row[11] else None,
                instructions=row[12]
            ))
        return tasks
    
    # Inventory operations
    def add_inventory_item(self, item: InventoryItem) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO inventory (
                name, part_number, category, system, location,
                current_stock, minimum_stock, supplier, unit_cost, specifications
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.name, item.part_number, item.category, item.system,
            item.location, item.current_stock, item.minimum_stock,
            item.supplier, item.unit_cost, item.specifications
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_inventory(self, low_stock_only: bool = False) -> List[InventoryItem]:
        cursor = self.conn.cursor()
        if low_stock_only:
            cursor.execute("""
                SELECT * FROM inventory 
                WHERE current_stock <= minimum_stock
            """)
        else:
            cursor.execute("SELECT * FROM inventory")
        return [InventoryItem(*row) for row in cursor.fetchall()]
    
    # Trip log operations
    def add_trip_log(self, log: TripLog) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO trip_logs (
                vessel_id, departure, arrival, origin, destination,
                start_hours, end_hours, fuel_used, distance, crew, log_entries
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            log.vessel_id, log.departure, log.arrival, log.origin,
            log.destination, log.start_hours, log.end_hours, log.fuel_used,
            log.distance, log.crew, log.log_entries
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    # Template operations
    def add_maintenance_template(self, template: MaintenanceTemplate) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO maintenance_templates (
                name, system, interval_hours, interval_days, instructions, parts_list
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            template.name, template.system, template.interval_hours,
            template.interval_days, template.instructions,
            json.dumps(template.parts_list) if template.parts_list else None
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    def backup_database(self, backup_path: str) -> bool:
        try:
            subprocess.run([
                "sqlite3", 
                self.db_path, 
                f".backup '{backup_path}'"
            ], check=True)
            return True
        except subprocess.CalledProcessError:
            return False

# ======================
# UI COMPONENTS
# ======================

class VesselEditor(Gtk.Dialog):
    def __init__(self, parent, db: Database, vessel: Optional[Vessel] = None):
        title = "Edit Vessel" if vessel else "Add Vessel"
        super().__init__(title=title, parent=parent, flags=0)
        self.db = db
        self.vessel = vessel
        self.set_default_size(600, 400)
        
        self._setup_ui()
    
    def _setup_ui(self):
        content = self.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        
        # Basic Information
        self.name_entry = self._add_form_field(grid, 0, "Name:", Gtk.Entry())
        self.reg_entry = self._add_form_field(grid, 1, "Registration:", Gtk.Entry())
        
        # Vessel Type
        self.type_combo = Gtk.ComboBoxText()
        for vtype in VESSEL_TYPES:
            self.type_combo.append_text(vtype)
        self._add_form_field(grid, 2, "Type:", self.type_combo)
        
        # Dimensions
        self.length_entry = self._add_form_field(grid, 3, "Length (m):", 
            Gtk.SpinButton.new_with_range(1, 200, 0.1))
        self.beam_entry = self._add_form_field(grid, 4, "Beam (m):", 
            Gtk.SpinButton.new_with_range(1, 50, 0.1))
        self.draft_entry = self._add_form_field(grid, 5, "Draft (m):", 
            Gtk.SpinButton.new_with_range(0, 20, 0.1))
        
        # Engine Info
        self.engine_entry = self._add_form_field(grid, 6, "Engine Model:", Gtk.Entry())
        self.hours_entry = self._add_form_field(grid, 7, "Engine Hours:", 
            Gtk.SpinButton.new_with_range(0, 100000, 1))
        
        # Other Details
        self.hull_combo = self._add_form_field(grid, 8, "Hull Material:", 
            Gtk.ComboBoxText.new_with_entry())
        for material in ["Fiberglass", "Aluminum", "Steel", "Wood", "Other"]:
            self.hull_combo.append_text(material)
        
        self.year_entry = self._add_form_field(grid, 9, "Build Year:", 
            Gtk.SpinButton.new_with_range(1900, datetime.now().year, 1))
        
        # Dates
        self.survey_entry = self._add_form_field(grid, 10, "Last Survey:", 
            Gtk.Entry(placeholder="YYYY-MM-DD"))
        self.insurance_entry = self._add_form_field(grid, 11, "Insurance Expiry:", 
            Gtk.Entry(placeholder="YYYY-MM-DD"))
        
        # Location and Notes
        self.location_entry = self._add_form_field(grid, 12, "Current Location:", Gtk.Entry())
        self.notes_entry = self._add_form_field(grid, 13, "Notes:", 
            Gtk.TextView(), multiline=True)
        
        # Load existing data if editing
        if self.vessel:
            self._load_existing_data()
        
        content.pack_start(grid, True, True, 0)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
    
    def _add_form_field(self, grid, row, label, widget, multiline=False):
        grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
        if multiline:
            scroll = Gtk.ScrolledWindow()
            scroll.set_min_content_height(100)
            scroll.add(widget)
            grid.attach(scroll, 1, row, 1, 1)
        else:
            grid.attach(widget, 1, row, 1, 1)
            return widget

def _load_existing_data(self):
    self.name_entry.set_text(self.vessel.name)
    self.reg_entry.set_text(self.vessel.registration)
    self.type_combo.set_active(VESSEL_TYPES.index(self.vessel.vessel_type))
    self.length_entry.set_value(self.vessel.length)
    self.beam_entry.set_value(self.vessel.beam)
    self.draft_entry.set_value(self.vessel.draft)
    self.engine_entry.set_text(self.vessel.engine_model)
    self.hours_entry.set_value(self.vessel.engine_hours)
    self.hull_combo.set_active(0)  # Default to first item
    self.year_entry.set_value(self.vessel.build_year)
    self.survey_entry.set_text(self.vessel.last_survey)
    self.insurance_entry.set_text(self.vessel.insurance_expiry)
    self.location_entry.set_text(self.vessel.current_location)
    
    buffer = self.notes_entry.get_buffer()
    buffer.set_text(self.vessel.notes)

def get_vessel_data(self) -> Vessel:
    buffer = self.notes_entry.get_buffer()
    start, end = buffer.get_bounds()
    
    return Vessel(
        id=self.vessel.id if self.vessel else None,
        name=self.name_entry.get_text(),
        registration=self.reg_entry.get_text(),
        vessel_type=self.type_combo.get_active_text(),
        length=self.length_entry.get_value(),
        beam=self.beam_entry.get_value(),
        draft=self.draft_entry.get_value(),
        engine_model=self.engine_entry.get_text(),
        engine_hours=self.hours_entry.get_value(),
        hull_material=self.hull_combo.get_active_text(),
        build_year=int(self.year_entry.get_value()),
        last_survey=self.survey_entry.get_text(),
        insurance_expiry=self.insurance_entry.get_text(),
        current_location=self.location_entry.get_text(),
        notes=buffer.get_text(start, end, False)
    )
class MaintenanceTaskEditor(Gtk.Dialog):

 def init(self, parent, db: Database, task: Optional[MaintenanceTask] = None):
             title = "Edit Task" if task else "Add Task"
            super().init(title=title, parent=parent, flags=0)
self.db = db
self.task = task
self.set_default_size(500, 400)
 self._setup_ui()

def _setup_ui(self):
    content = self.get_content_area()
    grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
    
    # Vessel selection
    self.vessel_combo = Gtk.ComboBoxText()
    vessels = self.db.get_vessels()
    for vessel in vessels:
        self.vessel_combo.append_text(f"{vessel.name} ({vessel.registration})")
    self._add_form_field(grid, 0, "Vessel:", self.vessel_combo)
    
    # Task details
    self.task_entry = self._add_form_field(grid, 1, "Task Name:", Gtk.Entry())
    self.system_combo = self._add_form_field(grid, 2, "System:", 
        Gtk.ComboBoxText.new_with_entry())
    for system in SYSTEM_CATEGORIES:
        self.system_combo.append_text(system)
    
    # Interval settings
    self.interval_type_combo = Gtk.ComboBoxText()
    for itype in IntervalType:
        self.interval_type_combo.append_text(itype.name)
    self._add_form_field(grid, 3, "Interval Type:", self.interval_type_combo)
    
    self.interval_value_entry = self._add_form_field(grid, 4, "Interval Value:", 
        Gtk.SpinButton.new_with_range(1, 365, 1))
    
    # Priority
    self.priority_combo = Gtk.ComboBoxText()
    for priority in Priority:
        self.priority_combo.append_text(priority.name)
    self._add_form_field(grid, 5, "Priority:", self.priority_combo)
    
    # Instructions
    self.instructions_entry = Gtk.TextView()
    self._add_form_field(grid, 6, "Instructions:", self.instructions_entry, multiline=True)
    
    # Load existing data if editing
    if self.task:
        self._load_existing_data()
    
    content.pack_start(grid, True, True, 0)
    self.add_button("Cancel", Gtk.ResponseType.CANCEL)
    self.add_button("Save", Gtk.ResponseType.OK)

def _add_form_field(self, grid, row, label, widget, multiline=False):
    grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
    if multiline:
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(100)
        scroll.add(widget)
        grid.attach(scroll, 1, row, 1, 1)
    else:
        grid.attach(widget, 1, row, 1, 1)
    return widget

def _load_existing_data(self):
    # Load existing task data into form
    pass

def get_task_data(self) -> MaintenanceTask:
    # Return MaintenanceTask object from form data
    pass
class VesselKeeperApp:
def init(self):
self.db = Database()
self.current_theme = "light"
# Main window setup
    self.window = Gtk.Window(title="VesselKeeper")
    self.window.set_default_size(1200, 800)
    self.window.connect("destroy", Gtk.main_quit)
    
    # Build UI
    self._setup_main_menu()
    self._setup_header_bar()
    self._setup_main_content()
    
    # Load initial data
    self._load_initial_data()

def _setup_main_menu(self):
    # Application menu setup
    pass

def _setup_header_bar(self):
    self.header = Gtk.HeaderBar()
    self.header.set_show_close_button(True)
    self.header.props.title = "VesselKeeper"
    self.window.set_titlebar(self.header)
    
    # Theme switcher
    self.theme_btn = Gtk.Button(label="🌙")
    self.theme_btn.connect("clicked", self._toggle_theme)
    self.header.pack_end(self.theme_btn)
    
    # Quick actions menu
    self.quick_menu = Gtk.MenuButton()
    menu = Gtk.Menu()
    
    # Add menu items
    add_vessel = Gtk.MenuItem(label="New Vessel")
    add_vessel.connect("activate", self._show_vessel_editor)
    menu.append(add_vessel)
    
    add_maintenance = Gtk.MenuItem(label="New Maintenance")
    add_maintenance.connect("activate", self._show_maintenance_editor)
    menu.append(add_maintenance)
    
    add_inventory = Gtk.MenuItem(label="New Inventory Item")
    add_inventory.connect("activate", self._show_inventory_editor)
    menu.append(add_inventory)
    
    menu.show_all()
    self.quick_menu.set_popup(menu)
    self.header.pack_start(self.quick_menu)

def _setup_main_content(self):
    self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
    
    # Sidebar with vessel list
    self.sidebar = self._create_sidebar()
    self.main_paned.pack1(self.sidebar, False, False)
    
    # Main notebook
    self.notebook = Gtk.Notebook()
    self._setup_dashboard_tab()
    self._setup_vessels_tab()
    self._setup_maintenance_tab()
    self._setup_inventory_tab()
    self._setup_trips_tab()
    self.main_paned.pack2(self.notebook, True, False)
    
    self.window.add(self.main_paned)

def _create_sidebar(self):
    sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    
    # Search box
    self.search_entry = Gtk.SearchEntry(placeholder_text="Search vessels...")
    self.search_entry.connect("search-changed", self._on_search)
    sidebar.pack_start(self.search_entry, False, False, 5)
    
    # Vessel list
    self.vessel_list = Gtk.ListBox()
    self.vessel_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
    self.vessel_list.connect("row-activated", self._on_vessel_selected)
    
    scroll = Gtk.ScrolledWindow()
    scroll.add(self.vessel_list)
    sidebar.pack_start(scroll, True, True, 0)
    
    return sidebar

def _setup_dashboard_tab(self):
    # Dashboard implementation
    pass

def _setup_vessels_tab(self):
    # Vessels tab implementation
    pass

def _setup_maintenance_tab(self):
    # Maintenance tab implementation
    pass

def _setup_inventory_tab(self):
    # Inventory tab implementation
    pass

def _setup_trips_tab(self):
    # Trips tab implementation
    pass

def _load_initial_data(self):
    # Load initial data
    pass

def _show_vessel_editor(self, widget):
    editor = VesselEditor(self.window, self.db)
    response = editor.run()
    if response == Gtk.ResponseType.OK:
        vessel = editor.get_vessel_data()
        if vessel.id:
            # Update existing vessel
            pass
        else:
            # Add new vessel
            self.db.add_vessel(vessel)
        self._load_initial_data()
    editor.destroy()

def _show_maintenance_editor(self, widget):
    editor = MaintenanceTaskEditor(self.window, self.db)
    response = editor.run()
    if response == Gtk.ResponseType.OK:
        task = editor.get_task_data()
        if task.id:
            # Update existing task
            pass
        else:
            # Add new task
            self.db.add_maintenance_task(task)
        self._load_initial_data()
    editor.destroy()

def _toggle_theme(self, widget):
    settings = Gtk.Settings.get_default()
    if self.current_theme == "light":
        settings.set_property("gtk-application-prefer-dark-theme", True)
        self.current_theme = "dark"
        widget.set_label("☀️")
    else:
        settings.set_property("gtk-application-prefer-dark-theme", False)
        self.current_theme = "light"
        widget.set_label("🌙")

def run(self):
    self.window.show_all()
    Gtk.main()
if name == "main":
app = VesselKeeperApp()
app.run()
