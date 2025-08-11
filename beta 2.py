#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import sqlite3
import os
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional
import subprocess

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
    engine_model: str = ""
    current_hours: float = 0.0
    notes: str = ""

@dataclass
class MaintenanceTask:
    id: Optional[int] = None
    vessel_id: int = 0
    task: str = ""
    interval_hours: int = 0
    last_completed: float = 0.0
    next_due: float = 0.0
    instructions: str = ""

@dataclass
class InventoryItem:
    id: Optional[int] = None
    name: str = ""
    part_number: str = ""
    quantity: int = 0
    location: str = ""
    threshold: int = 1
    supplier: str = ""

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
            name TEXT NOT NULL,
            registration TEXT,
            vessel_type TEXT,
            length REAL,
            engine_model TEXT,
            current_hours REAL DEFAULT 0,
            notes TEXT
        );
        
        CREATE TABLE IF NOT EXISTS maintenance (
            id INTEGER PRIMARY KEY,
            vessel_id INTEGER REFERENCES vessels(id),
            task TEXT NOT NULL,
            interval_hours INTEGER,
            last_completed REAL,
            next_due REAL,
            instructions TEXT
        );
        
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            part_number TEXT,
            quantity INTEGER DEFAULT 0,
            location TEXT,
            threshold INTEGER DEFAULT 1,
            supplier TEXT
        );
        """)
        self.conn.commit()
    
    # Vessel operations
    def add_vessel(self, vessel: Vessel) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO vessels (name, registration, vessel_type, length, engine_model, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (vessel.name, vessel.registration, vessel.vessel_type, 
              vessel.length, vessel.engine_model, vessel.notes))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_vessels(self) -> List[Vessel]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM vessels")
        return [Vessel(*row) for row in cursor.fetchall()]
    
    # Maintenance operations
    def add_maintenance(self, task: MaintenanceTask) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO maintenance (vessel_id, task, interval_hours, last_completed, next_due, instructions)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task.vessel_id, task.task, task.interval_hours, 
              task.last_completed, task.next_due, task.instructions))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_maintenance_tasks(self) -> List[MaintenanceTask]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM maintenance")
        return [MaintenanceTask(*row) for row in cursor.fetchall()]
    
    # Inventory operations
    def add_inventory(self, item: InventoryItem) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO inventory (name, part_number, quantity, location, threshold, supplier)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (item.name, item.part_number, item.quantity, 
              item.location, item.threshold, item.supplier))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_inventory(self) -> List[InventoryItem]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM inventory")
        return [InventoryItem(*row) for row in cursor.fetchall()]

# ======================
# MAIN APPLICATION
# ======================

class VesselKeeperApp:
    def __init__(self):
        self.db = Database()
        self.current_theme = "light"
        
        # Main window
        self.window = Gtk.Window(title="VesselKeeper")
        self.window.set_default_size(1024, 768)
        self.window.connect("destroy", Gtk.main_quit)
        
        self._setup_ui()
        self._load_data()
    
    def _setup_ui(self):
        # Main container
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.add(self.main_box)
        
        # Header bar
        self._setup_header_bar()
        
        # Notebook for tabs
        self.notebook = Gtk.Notebook()
        self._setup_vessels_tab()
        self._setup_maintenance_tab()
        self._setup_inventory_tab()
        self.main_box.pack_start(self.notebook, True, True, 0)
    
    def _setup_header_bar(self):
        self.header = Gtk.HeaderBar()
        self.header.set_show_close_button(True)
        self.header.props.title = "VesselKeeper"
        self.window.set_titlebar(self.header)
        
        # Theme switcher
        self.theme_btn = Gtk.Button(label="🌙")
        self.theme_btn.connect("clicked", self._toggle_theme)
        self.header.pack_end(self.theme_btn)
        
        # Menu button
        self.menu_btn = Gtk.MenuButton()
        menu = Gtk.Menu()
        
        # Vessels menu
        vessels_item = Gtk.MenuItem(label="Vessels")
        vessels_item.connect("activate", lambda x: self.notebook.set_current_page(0))
        menu.append(vessels_item)
        
        # Maintenance menu
        maint_item = Gtk.MenuItem(label="Maintenance")
        maint_item.connect("activate", lambda x: self.notebook.set_current_page(1))
        menu.append(maint_item)
        
        # Inventory menu
        inv_item = Gtk.MenuItem(label="Inventory")
        inv_item.connect("activate", lambda x: self.notebook.set_current_page(2))
        menu.append(inv_item)
        
        menu.show_all()
        self.menu_btn.set_popup(menu)
        self.header.pack_start(self.menu_btn)
    
    def _setup_vessels_tab(self):
        self.vessels_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        
        # Add vessel button
        add_btn = Gtk.Button(label="Add Vessel")
        add_btn.connect("clicked", self._show_vessel_editor)
        self.vessels_tab.pack_start(add_btn, False, False, 5)
        
        # Vessels list
        self.vessels_list = Gtk.ListBox()
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.vessels_list)
        self.vessels_tab.pack_start(scroll, True, True, 0)
        
        self.notebook.append_page(self.vessels_tab, Gtk.Label(label="Vessels"))
    
    def _setup_maintenance_tab(self):
        self.maintenance_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        
        # Add task button
        add_btn = Gtk.Button(label="Add Maintenance Task")
        add_btn.connect("clicked", self._show_maintenance_editor)
        self.maintenance_tab.pack_start(add_btn, False, False, 5)
        
        # Tasks list
        self.tasks_list = Gtk.ListBox()
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.tasks_list)
        self.maintenance_tab.pack_start(scroll, True, True, 0)
        
        self.notebook.append_page(self.maintenance_tab, Gtk.Label(label="Maintenance"))
    
    def _setup_inventory_tab(self):
        self.inventory_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        
        # Add item button
        add_btn = Gtk.Button(label="Add Inventory Item")
        add_btn.connect("clicked", self._show_inventory_editor)
        self.inventory_tab.pack_start(add_btn, False, False, 5)
        
        # Items list
        self.inventory_list = Gtk.ListBox()
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.inventory_list)
        self.inventory_tab.pack_start(scroll, True, True, 0)
        
        self.notebook.append_page(self.inventory_tab, Gtk.Label(label="Inventory"))
    
    def _load_data(self):
        # Load vessels
        for vessel in self.db.get_vessels():
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{vessel.name} ({vessel.registration})")
            row.add(label)
            self.vessels_list.add(row)
        
        # Load maintenance tasks
        for task in self.db.get_maintenance_tasks():
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{task.task} (Due: {task.next_due} hrs)")
            row.add(label)
            self.tasks_list.add(row)
        
        # Load inventory
        for item in self.db.get_inventory():
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{item.name} - Qty: {item.quantity}")
            row.add(label)
            self.inventory_list.add(row)
        
        self.vessels_list.show_all()
        self.tasks_list.show_all()
        self.inventory_list.show_all()
    
    def _show_vessel_editor(self, widget):
        dialog = Gtk.Dialog(title="Add Vessel", parent=self.window)
        dialog.set_default_size(400, 300)
        
        # Create form
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        
        # Name
        grid.attach(Gtk.Label(label="Name:"), 0, 0, 1, 1)
        name_entry = Gtk.Entry()
        grid.attach(name_entry, 1, 0, 1, 1)
        
        # Registration
        grid.attach(Gtk.Label(label="Registration:"), 0, 1, 1, 1)
        reg_entry = Gtk.Entry()
        grid.attach(reg_entry, 1, 1, 1, 1)
        
        # Type
        grid.attach(Gtk.Label(label="Type:"), 0, 2, 1, 1)
        type_combo = Gtk.ComboBoxText()
        for vtype in ["Motor Yacht", "Sailboat", "Fishing Vessel", "RIB"]:
            type_combo.append_text(vtype)
        grid.attach(type_combo, 1, 2, 1, 1)
        
        # Add to dialog
        content = dialog.get_content_area()
        content.pack_start(grid, True, True, 0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            vessel = Vessel(
                name=name_entry.get_text(),
                registration=reg_entry.get_text(),
                vessel_type=type_combo.get_active_text()
            )
            self.db.add_vessel(vessel)
            self._load_data()  # Refresh views
        
        dialog.destroy()
    
    def _show_maintenance_editor(self, widget):
        dialog = Gtk.Dialog(title="Add Maintenance Task", parent=self.window)
        dialog.set_default_size(400, 300)
        
        # Create form
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        
        # Task name
        grid.attach(Gtk.Label(label="Task:"), 0, 0, 1, 1)
        task_entry = Gtk.Entry()
        grid.attach(task_entry, 1, 0, 1, 1)
        
        # Interval
        grid.attach(Gtk.Label(label="Interval (hours):"), 0, 1, 1, 1)
        interval_entry = Gtk.SpinButton.new_with_range(1, 10000, 1)
        grid.attach(interval_entry, 1, 1, 1, 1)
        
        # Add to dialog
        content = dialog.get_content_area()
        content.pack_start(grid, True, True, 0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            task = MaintenanceTask(
                task=task_entry.get_text(),
                interval_hours=interval_entry.get_value_as_int()
            )
            self.db.add_maintenance(task)
            self._load_data()  # Refresh views
        
        dialog.destroy()
    
    def _show_inventory_editor(self, widget):
        dialog = Gtk.Dialog(title="Add Inventory Item", parent=self.window)
        dialog.set_default_size(400, 300)
        
        # Create form
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        
        # Name
        grid.attach(Gtk.Label(label="Name:"), 0, 0, 1, 1)
        name_entry = Gtk.Entry()
        grid.attach(name_entry, 1, 0, 1, 1)
        
        # Quantity
        grid.attach(Gtk.Label(label="Quantity:"), 0, 1, 1, 1)
        qty_entry = Gtk.SpinButton.new_with_range(0, 1000, 1)
        grid.attach(qty_entry, 1, 1, 1, 1)
        
        # Add to dialog
        content = dialog.get_content_area()
        content.pack_start(grid, True, True, 0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            item = InventoryItem(
                name=name_entry.get_text(),
                quantity=qty_entry.get_value_as_int()
            )
            self.db.add_inventory(item)
            self._load_data()  # Refresh views
        
        dialog.destroy()
    
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

if __name__ == "__main__":
    app = VesselKeeperApp()
    app.run()