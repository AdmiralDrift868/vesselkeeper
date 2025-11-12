#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MARINE ENGINEER PRO - Integrated Vessel Management System
Combines vessel management, marine engineering, and customer billing
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GObject, Pango

import sqlite3
import logging
import json
import csv
import datetime
import hashlib
import zipfile
import tempfile
import shutil
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
import threading
from pathlib import Path

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('marine_engineer_pro.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Integrated Enums ---
class MaintenancePriority(Enum):
    SAFETY_CRITICAL = "Safety Critical"
    OPERATIONAL_CRITICAL = "Operational Critical" 
    PREVENTATIVE = "Preventative"
    DEFERRED = "Deferred"
    COSMETIC = "Cosmetic"

class ComponentCondition(Enum):
    NEW = "New"
    EXCELLENT = "Excellent"
    GOOD = "Good"
    FAIR = "Fair"
    POOR = "Poor"
    CRITICAL = "Critical"
    FAILED = "Failed"

class WorkOrderStatus(Enum):
    DRAFT = "Draft"
    SCHEDULED = "Scheduled"
    IN_PROGRESS = "In Progress"
    AWAITING_PARTS = "Awaiting Parts"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    BILLED = "Billed"

class InvoiceStatus(Enum):
    DRAFT = "Draft"
    SENT = "Sent"
    PAID = "Paid"
    OVERDUE = "Overdue"
    CANCELLED = "Cancelled"

class PaymentMethod(Enum):
    CASH = "Cash"
    CHECK = "Check"
    BANK_TRANSFER = "Bank Transfer"
    CREDIT_CARD = "Credit Card"
    ONLINE = "Online"

class IntervalType(Enum):
    HOURS = "Hours"
    DAYS = "Days"
    CALENDAR_MONTHS = "Calendar Months"
    OPERATING_MONTHS = "Operating Months"

# --- Integrated Data Classes ---
@dataclass
class Vessel:
    id: Optional[int] = None
    name: str = ""
    vessel_type: str = ""
    length: float = 0.0
    beam: float = 0.0
    draft: float = 0.0
    engine_make: str = ""
    engine_model: str = ""
    engine_hours: float = 0.0
    hull_material: str = ""
    build_year: int = 0
    last_survey: str = ""
    insurance_expiry: str = ""
    current_location: str = ""
    notes: str = ""
    attachments: List[str] = field(default_factory=list)
    registration_number: str = ""
    home_port: str = ""
    gross_tonnage: float = 0.0
    image_path: str = ""
    owner_name: str = ""
    owner_email: str = ""
    owner_phone: str = ""

@dataclass
class MarineComponent:
    id: Optional[int] = None
    vessel_id: int = 0
    name: str = ""
    system: str = ""
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    installation_date: str = ""
    expected_life_hours: int = 0
    current_hours: float = 0.0
    condition: ComponentCondition = ComponentCondition.NEW
    last_inspection: str = ""
    next_inspection: str = ""
    maintenance_interval_hours: int = 0
    technical_specs: Dict[str, Any] = field(default_factory=dict)
    failure_modes: List[str] = field(default_factory=list)
    spare_parts: List[str] = field(default_factory=list)

@dataclass
class EngineeringWorkOrder:
    id: Optional[int] = None
    vessel_id: int = 0
    title: str = ""
    description: str = ""
    component_id: Optional[int] = None
    priority: MaintenancePriority = MaintenancePriority.PREVENTATIVE
    status: WorkOrderStatus = WorkOrderStatus.DRAFT
    assigned_engineer: str = ""
    estimated_hours: float = 0.0
    actual_hours: float = 0.0
    scheduled_date: str = ""
    completed_date: str = ""
    parts_required: List[Dict[str, Any]] = field(default_factory=list)
    labor_cost: Decimal = Decimal('0.00')
    parts_cost: Decimal = Decimal('0.00')
    total_cost: Decimal = Decimal('0.00')
    work_completed: str = ""
    findings: str = ""
    recommendations: str = ""
    safety_checks: List[str] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)

@dataclass
class InventoryItem:
    id: Optional[int] = None
    name: str = ""
    part_number: str = ""
    description: str = ""
    category: str = ""
    system: str = ""
    location: str = ""
    current_stock: int = 0
    minimum_stock: int = 0
    supplier_id: Optional[int] = None
    unit_cost: Decimal = Decimal('0.00')
    selling_price: Decimal = Decimal('0.00')
    specifications: str = ""
    attachments: List[str] = field(default_factory=list)
    reorder_quantity: int = 0
    last_reorder_date: str = ""

@dataclass
class Customer:
    id: Optional[int] = None
    name: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    tax_id: str = ""
    payment_terms: str = "Net 30"
    notes: str = ""
    vessels: List[int] = field(default_factory=list)

@dataclass
class Invoice:
    id: Optional[int] = None
    customer_id: int = 0
    vessel_id: int = 0
    work_order_id: int = 0
    invoice_number: str = ""
    issue_date: str = ""
    due_date: str = ""
    status: InvoiceStatus = InvoiceStatus.DRAFT
    items: List[Dict] = field(default_factory=list)
    subtotal: Decimal = Decimal('0.00')
    tax_rate: Decimal = Decimal('0.00')
    tax_amount: Decimal = Decimal('0.00')
    total: Decimal = Decimal('0.00')
    amount_paid: Decimal = Decimal('0.00')
    balance_due: Decimal = Decimal('0.00')
    notes: str = ""

@dataclass
class Payment:
    id: Optional[int] = None
    invoice_id: int = 0
    amount: Decimal = Decimal('0.00')
    payment_date: str = ""
    method: PaymentMethod = PaymentMethod.CASH
    reference: str = ""
    notes: str = ""

# --- Integrated Database Engine ---
class IntegratedDatabase:
    def __init__(self, db_path: str = "marine_engineer_pro.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._initialize_database()

    @contextmanager
    def get_cursor(self):
        self._lock.acquire()
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
            self._lock.release()

    def _initialize_database(self):
        with self.get_cursor() as cursor:
            # Vessels table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vessels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    vessel_type TEXT,
                    length REAL,
                    beam REAL,
                    draft REAL,
                    engine_make TEXT,
                    engine_model TEXT,
                    engine_hours REAL,
                    hull_material TEXT,
                    build_year INTEGER,
                    last_survey TEXT,
                    insurance_expiry TEXT,
                    current_location TEXT,
                    notes TEXT,
                    attachments TEXT,
                    registration_number TEXT,
                    home_port TEXT,
                    gross_tonnage REAL,
                    image_path TEXT,
                    owner_name TEXT,
                    owner_email TEXT,
                    owner_phone TEXT
                )
            """)

            # Marine Components table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS components (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    system TEXT,
                    manufacturer TEXT,
                    model TEXT,
                    serial_number TEXT,
                    installation_date TEXT,
                    expected_life_hours INTEGER,
                    current_hours REAL,
                    condition TEXT,
                    last_inspection TEXT,
                    next_inspection TEXT,
                    maintenance_interval_hours INTEGER,
                    technical_specs TEXT,
                    failure_modes TEXT,
                    spare_parts TEXT
                )
            """)

            # Work Orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS work_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT,
                    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
                    priority TEXT,
                    status TEXT,
                    assigned_engineer TEXT,
                    estimated_hours REAL,
                    actual_hours REAL,
                    scheduled_date TEXT,
                    completed_date TEXT,
                    parts_required TEXT,
                    labor_cost DECIMAL(10,2),
                    parts_cost DECIMAL(10,2),
                    total_cost DECIMAL(10,2),
                    work_completed TEXT,
                    findings TEXT,
                    recommendations TEXT,
                    safety_checks TEXT,
                    tools_required TEXT
                )
            """)

            # Inventory table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    part_number TEXT,
                    description TEXT,
                    category TEXT,
                    system TEXT,
                    location TEXT,
                    current_stock INTEGER,
                    minimum_stock INTEGER,
                    supplier_id INTEGER,
                    unit_cost DECIMAL(10,2),
                    selling_price DECIMAL(10,2),
                    specifications TEXT,
                    attachments TEXT,
                    reorder_quantity INTEGER,
                    last_reorder_date TEXT
                )
            """)

            # Customers table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    company TEXT,
                    email TEXT,
                    phone TEXT,
                    address TEXT,
                    tax_id TEXT,
                    payment_terms TEXT,
                    notes TEXT,
                    vessels TEXT
                )
            """)

            # Invoices table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
                    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                    work_order_id INTEGER REFERENCES work_orders(id) ON DELETE SET NULL,
                    invoice_number TEXT UNIQUE NOT NULL,
                    issue_date TEXT,
                    due_date TEXT,
                    status TEXT,
                    items TEXT,
                    subtotal DECIMAL(10,2),
                    tax_rate DECIMAL(5,2),
                    tax_amount DECIMAL(10,2),
                    total DECIMAL(10,2),
                    amount_paid DECIMAL(10,2),
                    balance_due DECIMAL(10,2),
                    notes TEXT
                )
            """)

            # Payments table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                    amount DECIMAL(10,2),
                    payment_date TEXT,
                    method TEXT,
                    reference TEXT,
                    notes TEXT
                )
            """)

    # Vessel methods
    def add_vessel(self, vessel: Vessel) -> int:
        with self.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO vessels (name, vessel_type, length, beam, draft, engine_make, engine_model,
                                     engine_hours, hull_material, build_year, last_survey, insurance_expiry,
                                     current_location, notes, attachments, registration_number, home_port, 
                                     gross_tonnage, image_path, owner_name, owner_email, owner_phone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                vessel.name, vessel.vessel_type, vessel.length, vessel.beam, vessel.draft,
                vessel.engine_make, vessel.engine_model, vessel.engine_hours, vessel.hull_material,
                vessel.build_year, vessel.last_survey, vessel.insurance_expiry, vessel.current_location,
                vessel.notes, json.dumps(vessel.attachments), vessel.registration_number,
                vessel.home_port, vessel.gross_tonnage, vessel.image_path,
                vessel.owner_name, vessel.owner_email, vessel.owner_phone
            ))
            return cursor.lastrowid

    def get_vessels(self) -> List[Vessel]:
        with self.get_cursor() as cursor:
            cursor.execute("SELECT * FROM vessels")
            return [self._row_to_vessel(row) for row in cursor.fetchall()]

    def _row_to_vessel(self, row) -> Vessel:
        return Vessel(
            id=row['id'],
            name=row['name'] or '',
            vessel_type=row['vessel_type'] or '',
            length=row['length'] or 0.0,
            beam=row['beam'] or 0.0,
            draft=row['draft'] or 0.0,
            engine_make=row['engine_make'] or '',
            engine_model=row['engine_model'] or '',
            engine_hours=row['engine_hours'] or 0.0,
            hull_material=row['hull_material'] or '',
            build_year=row['build_year'] or 0,
            last_survey=row['last_survey'] or '',
            insurance_expiry=row['insurance_expiry'] or '',
            current_location=row['current_location'] or '',
            notes=row['notes'] or '',
            attachments=json.loads(row['attachments']) if row['attachments'] else [],
            registration_number=row['registration_number'] or '',
            home_port=row['home_port'] or '',
            gross_tonnage=row['gross_tonnage'] or 0.0,
            image_path=row['image_path'] or '',
            owner_name=row['owner_name'] or '',
            owner_email=row['owner_email'] or '',
            owner_phone=row['owner_phone'] or ''
        )

    # Work Order methods
    def add_work_order(self, work_order: EngineeringWorkOrder) -> int:
        with self.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO work_orders (vessel_id, title, description, component_id, priority, status,
                                        assigned_engineer, estimated_hours, actual_hours, scheduled_date,
                                        completed_date, parts_required, labor_cost, parts_cost, total_cost,
                                        work_completed, findings, recommendations, safety_checks, tools_required)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                work_order.vessel_id, work_order.title, work_order.description, work_order.component_id,
                work_order.priority.value, work_order.status.value, work_order.assigned_engineer,
                work_order.estimated_hours, work_order.actual_hours, work_order.scheduled_date,
                work_order.completed_date, json.dumps(work_order.parts_required),
                float(work_order.labor_cost), float(work_order.parts_cost), float(work_order.total_cost),
                work_order.work_completed, work_order.findings, work_order.recommendations,
                json.dumps(work_order.safety_checks), json.dumps(work_order.tools_required)
            ))
            return cursor.lastrowid

    def get_work_orders(self) -> List[EngineeringWorkOrder]:
        with self.get_cursor() as cursor:
            cursor.execute("SELECT * FROM work_orders")
            return [self._row_to_work_order(row) for row in cursor.fetchall()]

    def _row_to_work_order(self, row) -> EngineeringWorkOrder:
        return EngineeringWorkOrder(
            id=row['id'],
            vessel_id=row['vessel_id'],
            title=row['title'] or '',
            description=row['description'] or '',
            component_id=row['component_id'],
            priority=MaintenancePriority(row['priority']) if row['priority'] else MaintenancePriority.PREVENTATIVE,
            status=WorkOrderStatus(row['status']) if row['status'] else WorkOrderStatus.DRAFT,
            assigned_engineer=row['assigned_engineer'] or '',
            estimated_hours=row['estimated_hours'] or 0.0,
            actual_hours=row['actual_hours'] or 0.0,
            scheduled_date=row['scheduled_date'] or '',
            completed_date=row['completed_date'] or '',
            parts_required=json.loads(row['parts_required']) if row['parts_required'] else [],
            labor_cost=Decimal(str(row['labor_cost'] or '0.00')),
            parts_cost=Decimal(str(row['parts_cost'] or '0.00')),
            total_cost=Decimal(str(row['total_cost'] or '0.00')),
            work_completed=row['work_completed'] or '',
            findings=row['findings'] or '',
            recommendations=row['recommendations'] or '',
            safety_checks=json.loads(row['safety_checks']) if row['safety_checks'] else [],
            tools_required=json.loads(row['tools_required']) if row['tools_required'] else []
        )

    # Invoice methods
    def add_invoice(self, invoice: Invoice) -> int:
        with self.get_cursor() as cursor:
            # Generate invoice number if not provided
            if not invoice.invoice_number:
                cursor.execute("SELECT COUNT(*) FROM invoices WHERE strftime('%Y', issue_date) = strftime('%Y', 'now')")
                count = cursor.fetchone()[0] + 1
                invoice.invoice_number = f"INV-{datetime.datetime.now().year}-{count:04d}"

            cursor.execute("""
                INSERT INTO invoices (customer_id, vessel_id, work_order_id, invoice_number, issue_date,
                                    due_date, status, items, subtotal, tax_rate, tax_amount, total,
                                    amount_paid, balance_due, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                invoice.customer_id, invoice.vessel_id, invoice.work_order_id, invoice.invoice_number,
                invoice.issue_date, invoice.due_date, invoice.status.value, json.dumps(invoice.items),
                float(invoice.subtotal), float(invoice.tax_rate), float(invoice.tax_amount),
                float(invoice.total), float(invoice.amount_paid), float(invoice.balance_due),
                invoice.notes
            ))
            return cursor.lastrowid

    def get_invoices(self) -> List[Invoice]:
        with self.get_cursor() as cursor:
            cursor.execute("SELECT * FROM invoices")
            return [self._row_to_invoice(row) for row in cursor.fetchall()]

    def _row_to_invoice(self, row) -> Invoice:
        return Invoice(
            id=row['id'],
            customer_id=row['customer_id'],
            vessel_id=row['vessel_id'],
            work_order_id=row['work_order_id'],
            invoice_number=row['invoice_number'] or '',
            issue_date=row['issue_date'] or '',
            due_date=row['due_date'] or '',
            status=InvoiceStatus(row['status']) if row['status'] else InvoiceStatus.DRAFT,
            items=json.loads(row['items']) if row['items'] else [],
            subtotal=Decimal(str(row['subtotal'] or '0.00')),
            tax_rate=Decimal(str(row['tax_rate'] or '0.00')),
            tax_amount=Decimal(str(row['tax_amount'] or '0.00')),
            total=Decimal(str(row['total'] or '0.00')),
            amount_paid=Decimal(str(row['amount_paid'] or '0.00')),
            balance_due=Decimal(str(row['balance_due'] or '0.00')),
            notes=row['notes'] or ''
        )

# --- Integrated UI Components ---
class DashboardTab(Gtk.Box):
    def __init__(self, db: IntegratedDatabase):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.db = db
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        
        self._setup_ui()
        self._refresh_data()

    def _setup_ui(self):
        # Header
        header = Gtk.Label()
        header.set_markup("<span size='x-large' weight='bold'>Marine Engineer Pro Dashboard</span>")
        header.set_xalign(0)
        self.pack_start(header, False, False, 0)

        # Statistics Grid
        stats_grid = Gtk.Grid()
        stats_grid.set_column_spacing(20)
        stats_grid.set_row_spacing(10)
        stats_grid.set_margin_top(10)
        stats_grid.set_margin_bottom(10)

        self.vessel_count_label = Gtk.Label(label="Vessels: 0")
        self.work_order_count_label = Gtk.Label(label="Work Orders: 0")
        self.invoice_count_label = Gtk.Label(label="Invoices: 0")
        self.revenue_label = Gtk.Label(label="Revenue: $0.00")

        stats_grid.attach(self.vessel_count_label, 0, 0, 1, 1)
        stats_grid.attach(self.work_order_count_label, 1, 0, 1, 1)
        stats_grid.attach(self.invoice_count_label, 2, 0, 1, 1)
        stats_grid.attach(self.revenue_label, 3, 0, 1, 1)

        self.pack_start(stats_grid, False, False, 0)

        # Recent Activity
        activity_frame = Gtk.Frame(label="Recent Activity")
        self.activity_store = Gtk.ListStore(str, str, str)  # Type, Description, Date
        activity_tree = Gtk.TreeView(model=self.activity_store)
        
        renderer = Gtk.CellRendererText()
        activity_tree.append_column(Gtk.TreeViewColumn("Type", renderer, text=0))
        activity_tree.append_column(Gtk.TreeViewColumn("Description", renderer, text=1))
        activity_tree.append_column(Gtk.TreeViewColumn("Date", renderer, text=2))
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(activity_tree)
        activity_frame.add(scrolled)
        
        self.pack_start(activity_frame, True, True, 0)

    def _refresh_data(self):
        vessels = self.db.get_vessels()
        work_orders = self.db.get_work_orders()
        invoices = self.db.get_invoices()
        
        self.vessel_count_label.set_text(f"Vessels: {len(vessels)}")
        self.work_order_count_label.set_text(f"Work Orders: {len(work_orders)}")
        self.invoice_count_label.set_text(f"Invoices: {len(invoices)}")
        
        total_revenue = sum(invoice.total for invoice in invoices if invoice.status == InvoiceStatus.PAID)
        self.revenue_label.set_text(f"Revenue: ${total_revenue:.2f}")
        
        # Update activity
        self.activity_store.clear()
        for work_order in work_orders[-10:]:  # Last 10 work orders
            self.activity_store.append([
                "Work Order",
                work_order.title,
                work_order.scheduled_date or "Not scheduled"
            ])
        
        for invoice in invoices[-10:]:  # Last 10 invoices
            self.activity_store.append([
                "Invoice",
                f"Invoice {invoice.invoice_number}",
                invoice.issue_date
            ])

class VesselManagementTab(Gtk.Box):
    def __init__(self, db: IntegratedDatabase):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.db = db
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        
        self._setup_ui()
        self._refresh_vessels()

    def _setup_ui(self):
        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        
        add_btn = Gtk.Button.new_with_label("Add Vessel")
        add_btn.connect("clicked", self._on_add_vessel)
        
        edit_btn = Gtk.Button.new_with_label("Edit Vessel")
        edit_btn.connect("clicked", self._on_edit_vessel)
        
        delete_btn = Gtk.Button.new_with_label("Delete Vessel")
        delete_btn.connect("clicked", self._on_delete_vessel)
        
        refresh_btn = Gtk.Button.new_with_label("Refresh")
        refresh_btn.connect("clicked", self._refresh_vessels)
        
        toolbar.pack_start(add_btn, False, False, 0)
        toolbar.pack_start(edit_btn, False, False, 0)
        toolbar.pack_start(delete_btn, False, False, 0)
        toolbar.pack_end(refresh_btn, False, False, 0)
        
        self.pack_start(toolbar, False, False, 0)

        # Vessels TreeView
        self.vessels_store = Gtk.ListStore(int, str, str, str, str, str)
        self.vessels_tree = Gtk.TreeView(model=self.vessels_store)
        
        columns = [
            ("ID", 50), ("Name", 150), ("Type", 120), 
            ("Engine", 150), ("Location", 120), ("Owner", 150)
        ]
        
        for i, (title, width) in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_min_width(width)
            self.vessels_tree.append_column(column)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.vessels_tree)
        
        self.pack_start(scrolled, True, True, 0)

    def _refresh_vessels(self, widget=None):
        self.vessels_store.clear()
        vessels = self.db.get_vessels()
        
        for vessel in vessels:
            self.vessels_store.append([
                vessel.id,
                vessel.name,
                vessel.vessel_type,
                f"{vessel.engine_make} {vessel.engine_model}",
                vessel.current_location,
                vessel.owner_name
            ])

    def _on_add_vessel(self, button):
        # Simplified - in real implementation, use a proper dialog
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Add Vessel functionality would go here"
        )
        dialog.run()
        dialog.destroy()

    def _on_edit_vessel(self, button):
        selection = self.vessels_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            vessel_id = model[treeiter][0]
            # Edit vessel logic here
            pass

    def _on_delete_vessel(self, button):
        selection = self.vessels_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            vessel_id = model[treeiter][0]
            # Delete vessel logic here
            pass

class WorkOrderTab(Gtk.Box):
    def __init__(self, db: IntegratedDatabase):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.db = db
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        
        self._setup_ui()
        self._refresh_work_orders()

    def _setup_ui(self):
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        
        add_btn = Gtk.Button.new_with_label("Add Work Order")
        add_btn.connect("clicked", self._on_add_work_order)
        
        complete_btn = Gtk.Button.new_with_label("Mark Complete")
        complete_btn.connect("clicked", self._on_complete_work_order)
        
        invoice_btn = Gtk.Button.new_with_label("Create Invoice")
        invoice_btn.connect("clicked", self._on_create_invoice)
        
        toolbar.pack_start(add_btn, False, False, 0)
        toolbar.pack_start(complete_btn, False, False, 0)
        toolbar.pack_start(invoice_btn, False, False, 0)
        
        self.pack_start(toolbar, False, False, 0)

        # Work Orders TreeView
        self.work_orders_store = Gtk.ListStore(int, str, str, str, str, str, str)
        self.work_orders_tree = Gtk.TreeView(model=self.work_orders_store)
        
        columns = [
            ("ID", 50), ("Title", 200), ("Vessel", 150),
            ("Status", 100), ("Priority", 100), ("Engineer", 120), ("Total Cost", 100)
        ]
        
        for i, (title, width) in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_min_width(width)
            self.work_orders_tree.append_column(column)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.work_orders_tree)
        
        self.pack_start(scrolled, True, True, 0)

    def _refresh_work_orders(self):
        self.work_orders_store.clear()
        work_orders = self.db.get_work_orders()
        vessels = self.db.get_vessels()
        
        vessel_dict = {vessel.id: vessel.name for vessel in vessels}
        
        for wo in work_orders:
            vessel_name = vessel_dict.get(wo.vessel_id, "Unknown")
            self.work_orders_store.append([
                wo.id,
                wo.title,
                vessel_name,
                wo.status.value,
                wo.priority.value,
                wo.assigned_engineer,
                f"${wo.total_cost:.2f}"
            ])

    def _on_add_work_order(self, button):
        # Add work order logic
        pass

    def _on_complete_work_order(self, button):
        # Complete work order logic
        pass

    def _on_create_invoice(self, button):
        selection = self.work_orders_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            work_order_id = model[treeiter][0]
            # Create invoice from work order
            work_orders = self.db.get_work_orders()
            work_order = next((wo for wo in work_orders if wo.id == work_order_id), None)
            
            if work_order:
                invoice = Invoice(
                    vessel_id=work_order.vessel_id,
                    work_order_id=work_order.id,
                    issue_date=datetime.datetime.now().strftime("%Y-%m-%d"),
                    due_date=(datetime.datetime.now() + datetime.timedelta(days=30)).strftime("%Y-%m-%d"),
                    items=[{
                        "description": f"Work Order: {work_order.title}",
                        "quantity": 1,
                        "unit_price": float(work_order.total_cost),
                        "amount": float(work_order.total_cost)
                    }],
                    subtotal=work_order.total_cost,
                    tax_rate=Decimal('0.00'),
                    tax_amount=Decimal('0.00'),
                    total=work_order.total_cost,
                    balance_due=work_order.total_cost
                )
                
                self.db.add_invoice(invoice)
                self._refresh_work_orders()

class BillingTab(Gtk.Box):
    def __init__(self, db: IntegratedDatabase):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.db = db
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)
        
        self._setup_ui()
        self._refresh_invoices()

    def _setup_ui(self):
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        
        add_btn = Gtk.Button.new_with_label("Create Invoice")
        add_btn.connect("clicked", self._on_add_invoice)
        
        pay_btn = Gtk.Button.new_with_label("Record Payment")
        pay_btn.connect("clicked", self._on_record_payment)
        
        export_btn = Gtk.Button.new_with_label("Export")
        export_btn.connect("clicked", self._on_export)
        
        toolbar.pack_start(add_btn, False, False, 0)
        toolbar.pack_start(pay_btn, False, False, 0)
        toolbar.pack_start(export_btn, False, False, 0)
        
        self.pack_start(toolbar, False, False, 0)

        # Invoices TreeView
        self.invoices_store = Gtk.ListStore(int, str, str, str, str, str, str)
        self.invoices_tree = Gtk.TreeView(model=self.invoices_store)
        
        columns = [
            ("ID", 50), ("Invoice #", 120), ("Customer", 150),
            ("Vessel", 120), ("Issue Date", 100), ("Total", 100), ("Status", 100)
        ]
        
        for i, (title, width) in enumerate(columns):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_min_width(width)
            self.invoices_tree.append_column(column)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.invoices_tree)
        
        self.pack_start(scrolled, True, True, 0)

    def _refresh_invoices(self):
        self.invoices_store.clear()
        invoices = self.db.get_invoices()
        vessels = self.db.get_vessels()
        
        vessel_dict = {vessel.id: vessel.name for vessel in vessels}
        
        for invoice in invoices:
            vessel_name = vessel_dict.get(invoice.vessel_id, "Unknown")
            self.invoices_store.append([
                invoice.id,
                invoice.invoice_number,
                f"Customer {invoice.customer_id}",  # In real app, get customer name
                vessel_name,
                invoice.issue_date,
                f"${invoice.total:.2f}",
                invoice.status.value
            ])

    def _on_add_invoice(self, button):
        # Add invoice logic
        pass

    def _on_record_payment(self, button):
        selection = self.invoices_tree.get_selection()
        model, treeiter = selection.get_selected()
        if treeiter:
            invoice_id = model[treeiter][0]
            # Record payment logic
            pass

    def _on_export(self, button):
        # Export invoices logic
        pass

# --- Main Application ---
class MarineEngineerPro(Gtk.Window):
    def __init__(self):
        super().__init__(title="Marine Engineer Pro - Integrated System")
        self.set_default_size(1400, 900)
        
        # Initialize database
        self.db = IntegratedDatabase()
        
        self._setup_ui()
        self._apply_styling()

    def _setup_ui(self):
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_vbox)

        # Header Bar
        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title("Marine Engineer Pro")
        header_bar.set_subtitle("Integrated Vessel Management & Billing System")
        self.set_titlebar(header_bar)

        # Main notebook
        self.notebook = Gtk.Notebook()
        main_vbox.pack_start(self.notebook, True, True, 0)

        # Create tabs
        self.dashboard_tab = DashboardTab(self.db)
        self.vessel_tab = VesselManagementTab(self.db)
        self.work_order_tab = WorkOrderTab(self.db)
        self.billing_tab = BillingTab(self.db)

        self.notebook.append_page(self.dashboard_tab, Gtk.Label(label="Dashboard"))
        self.notebook.append_page(self.vessel_tab, Gtk.Label(label="Vessels"))
        self.notebook.append_page(self.work_order_tab, Gtk.Label(label="Work Orders"))
        self.notebook.append_page(self.billing_tab, Gtk.Label(label="Billing"))
        self.notebook.append_page(Gtk.Label(label="Inventory"), Gtk.Label(label="Inventory"))
        self.notebook.append_page(Gtk.Label(label="Reports"), Gtk.Label(label="Reports"))

    def _apply_styling(self):
        css_provider = Gtk.CssProvider()
        css = """
        .critical { color: #dc3545; font-weight: bold; }
        .warning { color: #fd7e14; font-weight: bold; }
        .success { color: #28a745; }
        """
        css_provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

def main():
    # Create necessary directories
    Path('reports').mkdir(exist_ok=True)
    Path('backups').mkdir(exist_ok=True)
    
    app = MarineEngineerPro()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    
    logger.info("Marine Engineer Pro Integrated System started successfully")
    Gtk.main()

if __name__ == "__main__":
    main()