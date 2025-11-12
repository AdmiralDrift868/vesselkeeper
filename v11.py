#!/usr/bin/env python3
"""
MARINE ENGINEERING MAINTENANCE SYSTEM
Professional-grade maintenance tracking for marine engineers
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, GObject
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from enum import Enum
import sqlite3
import logging
import json

# --- MARINE ENGINEERING SPECIFIC ENUMS ---
class MaintenanceClass(Enum):
    """Classification of maintenance types per marine standards"""
    PLANNED_PREVENTIVE = "Planned Preventive"
    CONDITION_BASED = "Condition Based"
    BREAKDOWN = "Breakdown"
    EMERGENCY = "Emergency"
    DEFERRED = "Deferred"

class ComponentCriticality(Enum):
    """Marine component criticality classification"""
    CRITICAL_SAFETY = "Critical Safety"  # Loss of vessel/crew
    CRITICAL_OPERATIONAL = "Critical Operational"  # Loss of propulsion
    ESSENTIAL = "Essential"  # Reduced operational capability
    NON_ESSENTIAL = "Non-Essential"  # Comfort/convenience systems

class WorkOrderPriority(Enum):
    """Marine engineering work priorities"""
    EMERGENCY = "Emergency"  # Immediate action required
    URGENT = "Urgent"  # Within 24 hours
    HIGH = "High"  # Within 7 days
    NORMAL = "Normal"  # Scheduled maintenance
    LOW = "Low"  #Deferrable

# --- MARINE COMPONENT DATACLASSES ---
@dataclass
class MarineComponent:
    """Marine engineering component with maintenance requirements"""
    id: Optional[int] = None
    tag_number: str = ""  # Unique equipment tag
    name: str = ""  # Component name
    system: str = ""  # Engine, Steering, Electrical, etc.
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    installation_date: str = ""
    running_hours: float = 0.0
    criticality: ComponentCriticality = ComponentCriticality.NON_ESSENTIAL
    
    # Maintenance intervals (hours)
    inspection_interval: int = 0
    overhaul_interval: int = 0
    replacement_interval: int = 0
    
    # Current status
    last_inspection_date: str = ""
    last_overhaul_date: str = ""
    last_replacement_date: str = ""
    condition: str = "Operational"
    
    # Maintenance history
    total_maintenance_events: int = 0
    total_downtime_hours: float = 0.0

@dataclass
class MaintenanceTask:
    """Individual maintenance task with marine engineering focus"""
    id: Optional[int] = None
    component_id: int = 0
    work_order_id: Optional[int] = None
    task_description: str = ""
    maintenance_class: MaintenanceClass = MaintenanceClass.PLANNED_PREVENTIVE
    estimated_hours: float = 0.0
    actual_hours: float = 0.0
    parts_required: List[Dict] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)
    safety_requirements: List[str] = field(default_factory=list)
    technical_standards: List[str] = field(default_factory=list)  # SOLAS, Class rules, etc.
    completed: bool = False
    completion_date: Optional[str] = None
    certified_by: str = ""  # Engineer certification

@dataclass
class WorkOrder:
    """Marine engineering work order with proper workflow"""
    id: Optional[int] = None
    vessel_id: int = 0
    title: str = ""
    description: str = ""
    priority: WorkOrderPriority = WorkOrderPriority.NORMAL
    status: str = "Draft"
    
    # Engineering details
    raised_by: str = ""  # Officer/Engineer who identified issue
    assigned_to: str = ""  # Responsible engineer
    planned_start: Optional[str] = None
    planned_end: Optional[str] = None
    actual_start: Optional[str] = None
    actual_end: Optional[str] = None
    
    # Maintenance details
    components_affected: List[int] = field(default_factory=list)
    maintenance_tasks: List[MaintenanceTask] = field(default_factory=list)
    
    # Marine engineering specifics
    requires_dry_dock: bool = False
    requires_class_survey: bool = False
    safety_certification_required: bool = False
    
    # Cost tracking
    labor_cost: float = 0.0
    parts_cost: float = 0.0
    total_cost: float = 0.0

@dataclass
class MaintenanceHistory:
    """Complete maintenance history for analysis"""
    component_id: int
    work_order_id: int
    maintenance_type: str
    date_performed: str
    performed_by: str
    running_hours: float
    description: str
    parts_used: List[Dict]
    downtime_hours: float
    cost: float
    next_due_hours: float
    technician_notes: str

# --- MARINE MAINTENANCE CALCULATIONS ---
class MarineMaintenanceCalculator:
    """Calculates maintenance requirements based on marine engineering standards"""
    
    # Standard maintenance intervals (hours) for common marine components
    STANDARD_INTERVALS = {
        "MAIN_ENGINE": {
            "inspection": 250,
            "overhaul": 8000,
            "replacement": 24000
        },
        "GENERATOR": {
            "inspection": 500,
            "overhaul": 6000,
            "replacement": 18000
        },
        "PUMP": {
            "inspection": 1000,
            "overhaul": 8000,
            "replacement": 16000
        },
        "VALVE": {
            "inspection": 2000,
            "overhaul": 12000,
            "replacement": 24000
        }
    }
    
    @staticmethod
    def calculate_next_maintenance(component: MarineComponent, maintenance_type: str) -> float:
        """Calculate next due hours for maintenance based on component type and usage"""
        base_interval = MarineMaintenanceCalculator.STANDARD_INTERVALS.get(
            component.system.upper(), {}
        ).get(maintenance_type, 0)
        
        # Adjust based on criticality
        criticality_factor = {
            ComponentCriticality.CRITICAL_SAFETY: 0.8,      # More frequent
            ComponentCriticality.CRITICAL_OPERATIONAL: 0.9,
            ComponentCriticality.ESSENTIAL: 1.0,
            ComponentCriticality.NON_ESSENTIAL: 1.2         # Less frequent
        }
        
        adjusted_interval = base_interval * criticality_factor.get(component.criticality, 1.0)
        
        # Get last maintenance date
        last_maintenance_hours = 0.0
        if maintenance_type == "inspection":
            last_maintenance_hours = float(component.last_inspection_date or 0)
        elif maintenance_type == "overhaul":
            last_maintenance_hours = float(component.last_overhaul_date or 0)
        elif maintenance_type == "replacement":
            last_maintenance_hours = float(component.last_replacement_date or 0)
        
        return last_maintenance_hours + adjusted_interval
    
    @staticmethod
    def calculate_remaining_life(component: MarineComponent) -> Dict[str, float]:
        """Calculate remaining useful life for critical components"""
        current_hours = component.running_hours
        replacement_interval = MarineMaintenanceCalculator.calculate_next_maintenance(
            component, "replacement"
        )
        
        remaining_life_hours = max(0, replacement_interval - current_hours)
        remaining_life_percentage = (remaining_life_hours / replacement_interval * 100) if replacement_interval > 0 else 0
        
        return {
            "remaining_hours": remaining_life_hours,
            "remaining_percentage": remaining_life_percentage,
            "replacement_due_hours": replacement_interval
        }

# --- MARINE MAINTENANCE DATABASE ---
class MarineMaintenanceDB:
    """Database layer for marine maintenance tracking"""
    
    def __init__(self, db_path: str = "marine_maintenance.db"):
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Initialize marine maintenance database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Components table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_number TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                system TEXT NOT NULL,
                manufacturer TEXT,
                model TEXT,
                serial_number TEXT,
                installation_date TEXT,
                running_hours REAL DEFAULT 0,
                criticality TEXT,
                inspection_interval INTEGER,
                overhaul_interval INTEGER,
                replacement_interval INTEGER,
                last_inspection_date TEXT,
                last_overhaul_date TEXT,
                last_replacement_date TEXT,
                condition TEXT,
                total_maintenance_events INTEGER DEFAULT 0,
                total_downtime_hours REAL DEFAULT 0
            )
        """)
        
        # Work orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS work_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                priority TEXT,
                status TEXT,
                raised_by TEXT,
                assigned_to TEXT,
                planned_start TEXT,
                planned_end TEXT,
                actual_start TEXT,
                actual_end TEXT,
                requires_dry_dock BOOLEAN DEFAULT 0,
                requires_class_survey BOOLEAN DEFAULT 0,
                safety_certification_required BOOLEAN DEFAULT 0,
                labor_cost REAL DEFAULT 0,
                parts_cost REAL DEFAULT 0,
                total_cost REAL DEFAULT 0,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Maintenance tasks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id INTEGER,
                work_order_id INTEGER,
                task_description TEXT NOT NULL,
                maintenance_class TEXT,
                estimated_hours REAL,
                actual_hours REAL,
                parts_required TEXT,
                tools_required TEXT,
                safety_requirements TEXT,
                technical_standards TEXT,
                completed BOOLEAN DEFAULT 0,
                completion_date TEXT,
                certified_by TEXT,
                FOREIGN KEY (component_id) REFERENCES components (id),
                FOREIGN KEY (work_order_id) REFERENCES work_orders (id)
            )
        """)
        
        # Maintenance history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id INTEGER,
                work_order_id INTEGER,
                maintenance_type TEXT,
                date_performed TEXT,
                performed_by TEXT,
                running_hours REAL,
                description TEXT,
                parts_used TEXT,
                downtime_hours REAL,
                cost REAL,
                next_due_hours REAL,
                technician_notes TEXT,
                FOREIGN KEY (component_id) REFERENCES components (id),
                FOREIGN KEY (work_order_id) REFERENCES work_orders (id)
            )
        """)
        
        conn.commit()
        conn.close()

# --- MARINE MAINTENANCE SERVICE ---
class MarineMaintenanceService:
    """Core service for marine maintenance operations"""
    
    def __init__(self, db: MarineMaintenanceDB):
        self.db = db
        self.calculator = MarineMaintenanceCalculator()
    
    def get_due_maintenance(self) -> List[Dict]:
        """Get all maintenance tasks due based on running hours"""
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.id, c.tag_number, c.name, c.system, c.running_hours,
                c.last_inspection_date, c.last_overhaul_date, c.last_replacement_date,
                c.inspection_interval, c.overhaul_interval, c.replacement_interval
            FROM components c
            WHERE c.condition != 'Out of Service'
        """)
        
        due_maintenance = []
        for row in cursor.fetchall():
            component = MarineComponent(
                id=row[0], tag_number=row[1], name=row[2], system=row[3],
                running_hours=row[4], last_inspection_date=row[5],
                last_overhaul_date=row[6], last_replacement_date=row[7],
                inspection_interval=row[8], overhaul_interval=row[9],
                replacement_interval=row[10]
            )
            
            # Check each maintenance type
            for maint_type in ["inspection", "overhaul", "replacement"]:
                due_hours = self.calculator.calculate_next_maintenance(component, maint_type)
                if component.running_hours >= due_hours:
                    due_maintenance.append({
                        "component": component,
                        "maintenance_type": maint_type,
                        "due_hours": due_hours,
                        "overdue_hours": component.running_hours - due_hours
                    })
        
        conn.close()
        return due_maintenance
    
    def create_maintenance_work_order(self, component_id: int, maintenance_type: str) -> WorkOrder:
        """Create a work order for due maintenance"""
        # This would create a proper work order with standard tasks
        # based on maintenance type and component
        pass

# --- MARINE MAINTENANCE UI ---
class MaintenanceDashboard(Gtk.Box):
    """Dashboard showing maintenance status and due tasks"""
    
    def __init__(self, maintenance_service: MarineMaintenanceService):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.maintenance_service = maintenance_service
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        
        self._setup_ui()
        self._refresh_data()
    
    def _setup_ui(self):
        # Header
        header = Gtk.Label()
        header.set_markup("<span size='x-large' weight='bold'>Marine Maintenance Dashboard</span>")
        header.set_xalign(0)
        self.pack_start(header, False, False, 0)
        
        # Critical maintenance alerts
        critical_frame = Gtk.Frame(label="Critical Maintenance Due")
        self.critical_list = Gtk.ListBox()
        critical_frame.add(self.critical_list)
        self.pack_start(critical_frame, False, False, 0)
        
        # Maintenance overview
        overview_frame = Gtk.Frame(label="Maintenance Overview")
        overview_grid = Gtk.Grid()
        overview_grid.set_column_spacing(20)
        overview_grid.set_row_spacing(10)
        overview_grid.set_margin_start(10)
        overview_grid.set_margin_end(10)
        overview_grid.set_margin_top(10)
        overview_grid.set_margin_bottom(10)
        
        # Overview metrics
        self.total_components_label = Gtk.Label(label="Total Components: 0")
        self.due_maintenance_label = Gtk.Label(label="Due Maintenance: 0")
        self.critical_alerts_label = Gtk.Label(label="Critical Alerts: 0")
        
        overview_grid.attach(self.total_components_label, 0, 0, 1, 1)
        overview_grid.attach(self.due_maintenance_label, 1, 0, 1, 1)
        overview_grid.attach(self.critical_alerts_label, 2, 0, 1, 1)
        
        overview_frame.add(overview_grid)
        self.pack_start(overview_frame, False, False, 0)
    
    def _refresh_data(self):
        """Refresh dashboard data"""
        due_maintenance = self.maintenance_service.get_due_maintenance()
        
        # Update critical list
        self.critical_list.foreach(lambda widget: self.critical_list.remove(widget))
        
        for maintenance in due_maintenance:
            if maintenance["overdue_hours"] > 0:  # Actually overdue
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                
                component = maintenance["component"]
                label = Gtk.Label()
                label.set_markup(
                    f"<b>{component.name}</b> ({component.tag_number}) - "
                    f"{maintenance['maintenance_type'].title()} - "
                    f"<span foreground='red'>{maintenance['overdue_hours']:.0f} hours overdue</span>"
                )
                label.set_xalign(0)
                
                box.pack_start(label, True, True, 0)
                row.add(box)
                self.critical_list.add(row)
        
        self.critical_list.show_all()
        
        # Update overview metrics
        self.due_maintenance_label.set_text(f"Due Maintenance: {len(due_maintenance)}")

class ComponentMaintenanceView(Gtk.Box):
    """View for managing component maintenance"""
    
    def __init__(self, maintenance_service: MarineMaintenanceService):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.maintenance_service = maintenance_service
        
        self._setup_ui()
        self._load_components()
    
    def _setup_ui(self):
        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        
        refresh_btn = Gtk.Button.new_with_label("Refresh")
        refresh_btn.connect("clicked", self._load_components)
        
        add_component_btn = Gtk.Button.new_with_label("Add Component")
        add_component_btn.connect("clicked", self._add_component)
        
        toolbar.pack_start(refresh_btn, False, False, 0)
        toolbar.pack_start(add_component_btn, False, False, 0)
        
        self.pack_start(toolbar, False, False, 0)
        
        # Components treeview
        self.components_store = Gtk.ListStore(
            int, str, str, str, float, str, float, float, float  # id, tag, name, system, hours, condition, next_inspection, next_overhaul, next_replacement
        )
        
        self.treeview = Gtk.TreeView(model=self.components_store)
        
        # Add columns
        columns = [
            ("ID", 50),
            ("Tag", 100),
            ("Name", 150),
            ("System", 100),
            ("Hours", 80),
            ("Condition", 100),
            ("Next Insp", 80),
            ("Next Ovl", 80),
            ("Next Rep", 80)
        ]
        
        for i, (title, width) in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_min_width(width)
            self.treeview.append_column(column)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.treeview)
        
        self.pack_start(scrolled, True, True, 0)
    
    def _load_components(self, widget=None):
        """Load components from database"""
        self.components_store.clear()
        
        conn = sqlite3.connect(self.maintenance_service.db.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM components")
        
        for row in cursor.fetchall():
            component = MarineComponent(
                id=row[0], tag_number=row[1], name=row[2], system=row[3],
                running_hours=row[9] or 0, condition=row[17] or "Operational"
            )
            
            # Calculate next maintenance dates
            next_inspection = self.maintenance_service.calculator.calculate_next_maintenance(
                component, "inspection"
            )
            next_overhaul = self.maintenance_service.calculator.calculate_next_maintenance(
                component, "overhaul"
            )
            next_replacement = self.maintenance_service.calculator.calculate_next_maintenance(
                component, "replacement"
            )
            
            self.components_store.append([
                component.id,
                component.tag_number,
                component.name,
                component.system,
                component.running_hours,
                component.condition,
                next_inspection,
                next_overhaul,
                next_replacement
            ])
        
        conn.close()
    
    def _add_component(self, widget):
        """Open dialog to add new component"""
        dialog = ComponentEditor(self)
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            component = dialog.get_component()
            # Save to database
            self._save_component(component)
            self._load_components()
        
        dialog.destroy()
    
    def _save_component(self, component: MarineComponent):
        """Save component to database"""
        conn = sqlite3.connect(self.maintenance_service.db.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO components 
            (tag_number, name, system, manufacturer, model, serial_number, installation_date, criticality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            component.tag_number, component.name, component.system,
            component.manufacturer, component.model, component.serial_number,
            component.installation_date, component.criticality.value
        ))
        
        conn.commit()
        conn.close()

# --- MAIN APPLICATION ---
class MarineMaintenanceApp(Gtk.Window):
    """Main marine maintenance application"""
    
    def __init__(self):
        super().__init__(title="Marine Maintenance Manager")
        self.set_default_size(1200, 800)
        
        # Initialize services
        self.db = MarineMaintenanceDB()
        self.maintenance_service = MarineMaintenanceService(self.db)
        
        self._setup_ui()
    
    def _setup_ui(self):
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_box)
        
        # Notebook for different views
        self.notebook = Gtk.Notebook()
        main_box.pack_start(self.notebook, True, True, 0)
        
        # Add tabs
        dashboard = MaintenanceDashboard(self.maintenance_service)
        components_view = ComponentMaintenanceView(self.maintenance_service)
        
        self.notebook.append_page(dashboard, Gtk.Label(label="Dashboard"))
        self.notebook.append_page(components_view, Gtk.Label(label="Components"))
        self.notebook.append_page(Gtk.Label(label="Work Orders"), Gtk.Label(label="Work Orders"))
        self.notebook.append_page(Gtk.Label(label="History"), Gtk.Label(label="History"))

# --- RUN APPLICATION ---
if __name__ == "__main__":
    app = MarineMaintenanceApp()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()
