#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GObject, GLib
import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, List, Dict
import json
import subprocess
from enum import Enum

# ======================
# ENUMS & CONSTANTS
# ======================

class Priority(Enum):
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4

class IntervalType(Enum):
    HOURS = 1
    DAYS = 2
    USAGE_CYCLES = 3

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
        
        # Main tables
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
        """)
        
        # Create indexes for performance
        cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_vessels_name ON vessels(name);
        CREATE INDEX IF NOT EXISTS idx_maintenance_vessel ON maintenance_tasks(vessel_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_partno ON inventory(part_number);
        """)
        
        self.conn.commit()
    
    # [Previous CRUD methods updated with new fields...]
    
    def search_vessels(self, search_term: str) -> List[Vessel]:
        cursor = self.conn.cursor()
        query = """
            SELECT * FROM vessels 
            WHERE name LIKE ? OR registration LIKE ? OR engine_model LIKE ?
        """
        cursor.execute(query, (f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"))
        return [Vessel(*row) for row in cursor.fetchall()]
    
    def get_due_maintenance(self, days_ahead: int = 30) -> List[MaintenanceTask]:
        cursor = self.conn.cursor()
        query = """
            SELECT mt.*, v.name 
            FROM maintenance_tasks mt
            JOIN vessels v ON mt.vessel_id = v.id
            WHERE date(mt.next_due) BETWEEN date('now') AND date('now', ?)
            ORDER BY mt.priority, mt.next_due
        """
        cursor.execute(query, (f"+{days_ahead} days",))
        return [MaintenanceTask(*row[:12], 
                parts_required=json.loads(row[12]) if row[12] else [],
                instructions=row[13],
                vessel_name=row[14]) for row in cursor.fetchall()]

# ======================
# MAIN APPLICATION
# ======================

class VesselKeeperApp:
    def __init__(self):
        self.db = Database()
        self.current_theme = "light"
        
        # Main application window
        self.window = Gtk.Window(title="VesselKeeper")
        self.window.set_default_size(1200, 800)
        self.window.connect("destroy", Gtk.main_quit)
        
        # Setup UI components
        self._setup_main_menu()
        self._setup_header_bar()
        self._setup_main_content()
        
        # Load initial data
        self._load_initial_data()
    
    def _setup_main_menu(self):
        # [Menu implementation...]
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
        
        # Quick actions
        self.quick_add = Gtk.MenuButton()
        menu = Gtk.Menu()
        
        add_vessel = Gtk.MenuItem(label="New Vessel")
        add_vessel.connect("activate", self._show_vessel_editor)
        menu.append(add_vessel)
        
        # [Other menu items...]
        
        menu.show_all()
        self.quick_add.set_popup(menu)
        self.header.pack_start(self.quick_add)
    
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
        # [Dashboard implementation with stats and alerts...]
        pass
    
    def _setup_vessels_tab(self):
        # [Vessels management implementation...]
        pass
    
    def _setup_maintenance_tab(self):
        # [Maintenance system implementation...]
        pass
    
    def _setup_inventory_tab(self):
        # [Inventory management implementation...]
        pass
    
    def _setup_trips_tab(self):
        # [Trip logging implementation...]
        pass
    
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
    
    def _load_initial_data(self):
        # Load vessels into sidebar
        for vessel in self.db.get_vessels():
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            
            icon = Gtk.Image.new_from_icon_name(
                "boat-symbolic" if vessel.vessel_type == "Sailboat" else "ship-symbolic",
                Gtk.IconSize.BUTTON
            )
            
            label = Gtk.Label(label=vessel.name, xalign=0)
            box.pack_start(icon, False, False, 5)
            box.pack_start(label, True, True, 0)
            
            row.add(box)
            self.vessel_list.add(row)
        
        self.vessel_list.show_all()
    
    def _on_search(self, entry):
        # [Search functionality implementation...]
        pass
    
    def _on_vessel_selected(self, listbox, row):
        # [Vessel selection handler...]
        pass
    
    def run(self):
        self.window.show_all()
        Gtk.main()

if __name__ == "__main__":
    app = VesselKeeperApp()
    app.run()