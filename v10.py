#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marine Engineer Pro - Advanced vessel maintenance and operational management system
Built for professional marine engineers and mechanics with performance-critical operations
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GObject, Pango

import asyncio
import threading
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
from typing import Optional, List, Dict, Any, Tuple, Callable, Union
from enum import Enum
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import statistics
from decimal import Decimal, ROUND_HALF_UP

# --- Enhanced Logging for Marine Operations ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s',
    handlers=[
        logging.FileHandler('marine_engineer_pro.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Marine Engineering Specific Enums ---
class MaintenancePriority(Enum):
    SAFETY_CRITICAL = "Safety Critical"  # Must be addressed immediately
    OPERATIONAL_CRITICAL = "Operational Critical"  # Affects vessel operation
    PREVENTATIVE = "Preventative"  # Scheduled maintenance
    DEFERRED = "Deferred"  # Can be postponed
    COSMETIC = "Cosmetic"  # Non-essential

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

class FuelType(Enum):
    MARINE_DIESEL = "Marine Diesel"
    GASOLINE = "Gasoline"
    LPG = "LPG"
    ELECTRIC = "Electric"
    HYBRID = "Hybrid"

class SystemCriticality(Enum):
    SAFETY = "Safety"  # Life-saving equipment
    PROPULSION = "Propulsion"  # Engine, transmission, propulsion
    NAVIGATION = "Navigation"  # GPS, radar, communications
    AUXILIARY = "Auxiliary"  # Non-essential systems
    COMFORT = "Comfort"  # Amenities

# --- Marine Engineering Data Classes ---
@dataclass
class MarineComponent:
    """Represents a critical marine component with engineering specs"""
    id: Optional[int] = None
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
    criticality: SystemCriticality = SystemCriticality.AUXILIARY
    technical_specs: Dict[str, Any] = field(default_factory=dict)
    failure_modes: List[str] = field(default_factory=list)
    spare_parts: List[str] = field(default_factory=list)

@dataclass
class EngineeringWorkOrder:
    """Professional work order for marine maintenance"""
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
    pre_work_photos: List[str] = field(default_factory=list)
    post_work_photos: List[str] = field(default_factory=list)
    work_completed: str = ""
    findings: str = ""
    recommendations: str = ""
    safety_checks: List[str] = field(default_factory=list)
    tools_required: List[str] = field(default_factory=list)
    lockout_tagout_required: bool = False

@dataclass
class FuelConsumptionRecord:
    """Detailed fuel consumption tracking for marine engines"""
    id: Optional[int] = None
    vessel_id: int = 0
    engine_hours_start: float = 0.0
    engine_hours_end: float = 0.0
    fuel_quantity: Decimal = Decimal('0.00')
    fuel_type: FuelType = FuelType.MARINE_DIESEL
    fuel_cost: Decimal = Decimal('0.00')
    date: str = ""
    location: str = ""
    fuel_density: Optional[Decimal] = None
    temperature: Optional[float] = None
    notes: str = ""

@dataclass
class ConditionMonitoringData:
    """Vibration, temperature, and performance monitoring"""
    id: Optional[int] = None
    component_id: int = 0
    measurement_date: str = ""
    vibration_axial: Optional[float] = None  # mm/s
    vibration_radial: Optional[float] = None  # mm/s
    temperature: Optional[float] = None  # °C
    pressure: Optional[float] = None  # bar
    flow_rate: Optional[float] = None  # l/min
    electrical_current: Optional[float] = None  # amps
    notes: str = ""
    alert_level: str = "Normal"  # Normal, Watch, Alert, Critical

@dataclass
class MarineVessel:
    """Enhanced vessel information for engineering purposes"""
    id: Optional[int] = None
    name: str = ""
    imo_number: Optional[str] = None
    call_sign: str = ""
    vessel_type: str = ""
    gross_tonnage: Decimal = Decimal('0.00')
    net_tonnage: Decimal = Decimal('0.00')
    length_overall: Decimal = Decimal('0.00')
    beam: Decimal = Decimal('0.00')
    draft: Decimal = Decimal('0.00')
    build_year: int = 0
    hull_material: str = ""
    classification_society: str = ""
    port_of_registry: str = ""
    main_engine_model: str = ""
    main_engine_power: Decimal = Decimal('0.00')  # kW
    generator_models: List[str] = field(default_factory=list)
    fuel_capacity: Decimal = Decimal('0.00')
    fresh_water_capacity: Decimal = Decimal('0.00')
    last_dry_dock: str = ""
    next_dry_dock: str = ""
    special_survey_due: str = ""
    current_location: str = ""
    operational_status: str = "Active"

# --- High-Performance Database Engine ---
class MarineDatabaseEngine:
    """Thread-safe, high-performance database engine for marine operations"""
    
    def __init__(self, db_path: str = "marine_engineer_pro.db"):
        self.db_path = Path(db_path)
        self._connection_pool = {}
        self._lock = threading.RLock()
        self._thread_pool = ThreadPoolExecutor(max_workers=4)
        self._initialize_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-specific database connection"""
        thread_id = threading.get_ident()
        if thread_id not in self._connection_pool:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
            self._connection_pool[thread_id] = conn
        return self._connection_pool[thread_id]
    
    @contextmanager
    def transaction(self):
        """Thread-safe transaction context manager"""
        conn = self._get_connection()
        with self._lock:
            try:
                yield conn.cursor()
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Transaction failed: {e}")
                raise
    
    def _initialize_database(self):
        """Initialize marine engineering database schema"""
        with self.transaction() as cursor:
            # Marine Vessels Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vessels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    imo_number TEXT UNIQUE,
                    call_sign TEXT,
                    vessel_type TEXT,
                    gross_tonnage DECIMAL(10,2),
                    net_tonnage DECIMAL(10,2),
                    length_overall DECIMAL(8,2),
                    beam DECIMAL(8,2),
                    draft DECIMAL(8,2),
                    build_year INTEGER,
                    hull_material TEXT,
                    classification_society TEXT,
                    port_of_registry TEXT,
                    main_engine_model TEXT,
                    main_engine_power DECIMAL(8,2),
                    generator_models TEXT,
                    fuel_capacity DECIMAL(8,2),
                    fresh_water_capacity DECIMAL(8,2),
                    last_dry_dock TEXT,
                    next_dry_dock TEXT,
                    special_survey_due TEXT,
                    current_location TEXT,
                    operational_status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Marine Components Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS components (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    system TEXT NOT NULL,
                    manufacturer TEXT,
                    model TEXT,
                    serial_number TEXT,
                    installation_date TEXT,
                    expected_life_hours INTEGER,
                    current_hours DECIMAL(10,2),
                    condition TEXT,
                    last_inspection TEXT,
                    next_inspection TEXT,
                    maintenance_interval_hours INTEGER,
                    criticality TEXT,
                    technical_specs TEXT,
                    failure_modes TEXT,
                    spare_parts TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Engineering Work Orders Table
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
                    estimated_hours DECIMAL(6,2),
                    actual_hours DECIMAL(6,2),
                    scheduled_date TEXT,
                    completed_date TEXT,
                    parts_required TEXT,
                    labor_cost DECIMAL(10,2),
                    parts_cost DECIMAL(10,2),
                    total_cost DECIMAL(10,2),
                    pre_work_photos TEXT,
                    post_work_photos TEXT,
                    work_completed TEXT,
                    findings TEXT,
                    recommendations TEXT,
                    safety_checks TEXT,
                    tools_required TEXT,
                    lockout_tagout_required BOOLEAN,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Fuel Consumption Tracking
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fuel_consumption (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vessel_id INTEGER REFERENCES vessels(id) ON DELETE CASCADE,
                    engine_hours_start DECIMAL(10,2),
                    engine_hours_end DECIMAL(10,2),
                    fuel_quantity DECIMAL(8,2),
                    fuel_type TEXT,
                    fuel_cost DECIMAL(8,2),
                    date TEXT,
                    location TEXT,
                    fuel_density DECIMAL(6,4),
                    temperature DECIMAL(4,1),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Condition Monitoring Data
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS condition_monitoring (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component_id INTEGER REFERENCES components(id) ON DELETE CASCADE,
                    measurement_date TEXT,
                    vibration_axial DECIMAL(6,3),
                    vibration_radial DECIMAL(6,3),
                    temperature DECIMAL(5,1),
                    pressure DECIMAL(6,2),
                    flow_rate DECIMAL(6,2),
                    electrical_current DECIMAL(6,2),
                    notes TEXT,
                    alert_level TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create performance indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_components_vessel ON components(vessel_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_status ON work_orders(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_work_orders_priority ON work_orders(priority)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fuel_vessel_date ON fuel_consumption(vessel_id, date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_monitoring_component ON condition_monitoring(component_id)")

# --- Marine Engineering Services ---
class MarineEngineeringService:
    """Core business logic for marine engineering operations"""
    
    def __init__(self, db_engine: MarineDatabaseEngine):
        self.db = db_engine
        self._component_wear_rates = {}  # Cache for component wear calculations
    
    def calculate_component_health(self, component: MarineComponent) -> Dict[str, Any]:
        """Calculate component health based on usage and condition monitoring"""
        with self.db.transaction() as cursor:
            # Get recent condition monitoring data
            cursor.execute("""
                SELECT * FROM condition_monitoring 
                WHERE component_id = ? 
                ORDER BY measurement_date DESC LIMIT 10
            """, (component.id,))
            monitoring_data = cursor.fetchall()
            
            if not monitoring_data:
                return {"health_score": 0.8, "confidence": "Low", "recommendation": "Collect monitoring data"}
            
            # Calculate health score based on multiple factors
            health_factors = []
            
            # Hours-based wear
            if component.expected_life_hours > 0:
                hours_used_ratio = component.current_hours / component.expected_life_hours
                hours_health = max(0, 1 - hours_used_ratio)
                health_factors.append(hours_health * 0.4)  # 40% weight
            
            # Vibration analysis
            recent_vibrations = [row['vibration_axial'] or 0 for row in monitoring_data if row['vibration_axial']]
            if recent_vibrations:
                avg_vibration = statistics.mean(recent_vibrations)
                vibration_health = max(0, 1 - (avg_vibration / 10))  # Normalize to 0-1
                health_factors.append(vibration_health * 0.3)  # 30% weight
            
            # Temperature analysis
            recent_temps = [row['temperature'] or 0 for row in monitoring_data if row['temperature']]
            if recent_temps:
                avg_temp = statistics.mean(recent_temps)
                temp_health = max(0, 1 - ((avg_temp - 60) / 40))  # Assume 60°C optimal
                health_factors.append(temp_health * 0.2)  # 20% weight
            
            # Condition rating
            condition_weights = {
                ComponentCondition.NEW: 1.0,
                ComponentCondition.EXCELLENT: 0.9,
                ComponentCondition.GOOD: 0.7,
                ComponentCondition.FAIR: 0.5,
                ComponentCondition.POOR: 0.3,
                ComponentCondition.CRITICAL: 0.1,
                ComponentCondition.FAILED: 0.0
            }
            health_factors.append(condition_weights.get(component.condition, 0.5) * 0.1)  # 10% weight
            
            health_score = sum(health_factors) / len(health_factors)
            
            # Determine confidence and recommendations
            if len(monitoring_data) >= 5:
                confidence = "High"
            elif len(monitoring_data) >= 2:
                confidence = "Medium"
            else:
                confidence = "Low"
            
            recommendation = self._generate_maintenance_recommendation(health_score, component)
            
            return {
                "health_score": round(health_score, 3),
                "confidence": confidence,
                "recommendation": recommendation,
                "next_inspection_hours": self._calculate_next_inspection(component, health_score)
            }
    
    def _generate_maintenance_recommendation(self, health_score: float, component: MarineComponent) -> str:
        """Generate maintenance recommendations based on health score"""
        if health_score >= 0.8:
            return "Continue normal operation and monitoring"
        elif health_score >= 0.6:
            return "Schedule preventative maintenance within next 200 operating hours"
        elif health_score >= 0.4:
            return "Schedule maintenance within next 50 operating hours"
        elif health_score >= 0.2:
            return "Immediate maintenance required - monitor closely"
        else:
            return "CRITICAL - Immediate maintenance required, consider replacement"
    
    def _calculate_next_inspection(self, component: MarineComponent, health_score: float) -> int:
        """Calculate recommended hours until next inspection"""
        base_interval = component.maintenance_interval_hours or 500
        health_adjustment = health_score * 0.5 + 0.5  # 0.5 to 1.0 multiplier
        return int(base_interval * health_adjustment)
    
    def analyze_fuel_efficiency(self, vessel_id: int, period_days: int = 30) -> Dict[str, Any]:
        """Analyze fuel efficiency and consumption patterns"""
        with self.db.transaction() as cursor:
            cursor.execute("""
                SELECT * FROM fuel_consumption 
                WHERE vessel_id = ? AND date >= date('now', '-' || ? || ' days')
                ORDER BY date
            """, (vessel_id, period_days))
            fuel_data = cursor.fetchall()
            
            if not fuel_data:
                return {"error": "No fuel data available for analysis"}
            
            # Calculate efficiency metrics
            total_fuel = sum(Decimal(str(row['fuel_quantity'])) for row in fuel_data)
            total_hours = sum(row['engine_hours_end'] - row['engine_hours_start'] for row in fuel_data)
            total_cost = sum(Decimal(str(row['fuel_cost'])) for row in fuel_data)
            
            if total_hours == 0:
                return {"error": "No engine hours recorded"}
            
            avg_fuel_consumption = float(total_fuel) / total_hours
            avg_cost_per_hour = float(total_cost) / total_hours
            
            # Calculate trends
            daily_consumption = {}
            for row in fuel_data:
                date = row['date']
                fuel = float(row['fuel_quantity'])
                hours = row['engine_hours_end'] - row['engine_hours_start']
                if date not in daily_consumption:
                    daily_consumption[date] = {'fuel': 0, 'hours': 0}
                daily_consumption[date]['fuel'] += fuel
                daily_consumption[date]['hours'] += hours
            
            daily_efficiency = {
                date: data['fuel'] / data['hours'] if data['hours'] > 0 else 0
                for date, data in daily_consumption.items()
            }
            
            efficiency_trend = "Stable"
            if len(daily_efficiency) > 1:
                efficiencies = list(daily_efficiency.values())
                first_half = statistics.mean(efficiencies[:len(efficiencies)//2])
                second_half = statistics.mean(efficiencies[len(efficiencies)//2:])
                if second_half > first_half * 1.1:
                    efficiency_trend = "Deteriorating"
                elif second_half < first_half * 0.9:
                    efficiency_trend = "Improving"
            
            return {
                "analysis_period_days": period_days,
                "total_fuel_used": float(total_fuel),
                "total_operating_hours": total_hours,
                "average_fuel_consumption_lph": round(avg_fuel_consumption, 2),
                "average_cost_per_hour": round(avg_cost_per_hour, 2),
                "total_fuel_cost": float(total_cost),
                "efficiency_trend": efficiency_trend,
                "recommendations": self._generate_fuel_efficiency_recommendations(avg_fuel_consumption, efficiency_trend)
            }
    
    def _generate_fuel_efficiency_recommendations(self, consumption: float, trend: str) -> List[str]:
        """Generate fuel efficiency improvement recommendations"""
        recommendations = []
        
        if consumption > 20:  # High consumption threshold
            recommendations.append("Consider engine tuning and injector service")
            recommendations.append("Check propeller condition and hull cleanliness")
        
        if trend == "Deteriorating":
            recommendations.append("Immediate engine performance check recommended")
            recommendations.append("Review operating parameters and load conditions")
        
        if not recommendations:
            recommendations.append("Fuel efficiency within expected parameters")
        
        return recommendations

# --- High-Performance UI Components ---
class MarineEngineeringDashboard(Gtk.Box):
    """Real-time dashboard for marine engineering operations"""
    
    def __init__(self, engineering_service: MarineEngineeringService):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.engineering_service = engineering_service
        self.set_margin_start(10)
        self.set_margin_end(10)
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        
        self.health_monitors = {}
        self._setup_ui()
        self._start_real_time_updates()
    
    def _setup_ui(self):
        # Header
        header = Gtk.Label()
        header.set_markup("<span size='x-large' weight='bold'>Marine Engineering Dashboard</span>")
        header.set_xalign(0)
        self.pack_start(header, False, False, 0)
        
        # Critical Alerts Frame
        alerts_frame = Gtk.Frame(label="Critical Alerts")
        self.alerts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        alerts_frame.add(self.alerts_box)
        self.pack_start(alerts_frame, False, False, 0)
        
        # Health Monitoring Grid
        health_frame = Gtk.Frame(label="Component Health Monitoring")
        self.health_grid = Gtk.Grid()
        self.health_grid.set_column_spacing(10)
        self.health_grid.set_row_spacing(5)
        self.health_grid.set_margin_start(10)
        self.health_grid.set_margin_end(10)
        self.health_grid.set_margin_top(10)
        self.health_grid.set_margin_bottom(10)
        
        scrolled_health = Gtk.ScrolledWindow()
        scrolled_health.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_health.add(self.health_grid)
        health_frame.add(scrolled_health)
        self.pack_start(health_frame, True, True, 0)
    
    def _start_real_time_updates(self):
        """Start real-time dashboard updates"""
        def update_dashboard():
            try:
                self._update_alerts()
                self._update_health_monitors()
            except Exception as e:
                logger.error(f"Dashboard update error: {e}")
            return True  # Continue updates
        
        # Update every 30 seconds
        GLib.timeout_add_seconds(30, update_dashboard)
    
    def _update_alerts(self):
        """Update critical alerts display"""
        # Clear existing alerts
        for child in self.alerts_box.get_children():
            self.alerts_box.remove(child)
        
        # TODO: Implement actual alert checking
        # Placeholder for critical alerts logic
        critical_alerts = [
            "Main engine vibration levels approaching critical",
            "Port generator requires immediate service",
            "Fuel filter differential pressure high"
        ]
        
        for alert in critical_alerts:
            alert_label = Gtk.Label(label=f"⚠️ {alert}")
            alert_label.set_xalign(0)
            self.alerts_box.pack_start(alert_label, False, False, 0)
        
        self.alerts_box.show_all()
    
    def _update_health_monitors(self):
        """Update component health monitoring display"""
        # TODO: Implement actual health monitoring
        # This would connect to the engineering service for real data
        pass

class ComponentHealthWidget(Gtk.Box):
    """Widget for displaying component health with visual indicators"""
    
    def __init__(self, component: MarineComponent, health_data: Dict[str, Any]):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        
        # Health indicator (color-coded)
        health_indicator = Gtk.DrawingArea()
        health_indicator.set_size_request(20, 60)
        health_indicator.connect("draw", self._draw_health_indicator, health_data['health_score'])
        
        # Component info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name_label = Gtk.Label(label=component.name)
        name_label.set_xalign(0)
        name_label.set_markup(f"<b>{component.name}</b>")
        
        system_label = Gtk.Label(label=f"System: {component.system}")
        system_label.set_xalign(0)
        
        health_label = Gtk.Label(label=f"Health: {health_data['health_score']:.1%}")
        health_label.set_xalign(0)
        
        recommendation_label = Gtk.Label(label=health_data['recommendation'])
        recommendation_label.set_xalign(0)
        recommendation_label.set_line_wrap(True)
        recommendation_label.set_max_width_chars(40)
        
        info_box.pack_start(name_label, False, False, 0)
        info_box.pack_start(system_label, False, False, 0)
        info_box.pack_start(health_label, False, False, 0)
        info_box.pack_start(recommendation_label, False, False, 0)
        
        self.pack_start(health_indicator, False, False, 0)
        self.pack_start(info_box, True, True, 0)
    
    def _draw_health_indicator(self, widget, cr, health_score):
        """Draw color-coded health indicator"""
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        
        # Determine color based on health score
        if health_score >= 0.8:
            color = (0, 0.8, 0)  # Green
        elif health_score >= 0.6:
            color = (0.8, 0.8, 0)  # Yellow
        elif health_score >= 0.4:
            color = (1.0, 0.5, 0)  # Orange
        else:
            color = (0.8, 0, 0)  # Red
        
        # Draw background
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        
        # Draw health bar
        bar_height = height * health_score
        cr.set_source_rgb(*color)
        cr.rectangle(0, height - bar_height, width, bar_height)
        cr.fill()
        
        # Draw border
        cr.set_source_rgb(0.5, 0.5, 0.5)
        cr.rectangle(0, 0, width, height)
        cr.stroke()

# --- Main Application Window ---
class MarineEngineerPro(Gtk.Window):
    """Main application window for Marine Engineer Pro"""
    
    def __init__(self):
        super().__init__(title="Marine Engineer Pro")
        self.set_default_size(1400, 900)
        
        # Initialize core services
        self.db_engine = MarineDatabaseEngine()
        self.engineering_service = MarineEngineeringService(self.db_engine)
        
        self._setup_ui()
        self._apply_professional_styling()
        self._initialize_async_operations()
    
    def _setup_ui(self):
        # Main container
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(main_vbox)
        
        # Professional header bar
        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title("Marine Engineer Pro")
        header_bar.set_subtitle("Professional Vessel Maintenance Management")
        self.set_titlebar(header_bar)
        
        # Add professional menu buttons
        menu_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        dashboard_btn = Gtk.Button.new_with_label("Dashboard")
        maintenance_btn = Gtk.Button.new_with_label("Maintenance")
        inventory_btn = Gtk.Button.new_with_label("Inventory")
        reports_btn = Gtk.Button.new_with_label("Reports")
        
        dashboard_btn.connect("clicked", self._show_dashboard)
        maintenance_btn.connect("clicked", self._show_maintenance)
        inventory_btn.connect("clicked", self._show_inventory)
        reports_btn.connect("clicked", self._show_reports)
        
        menu_box.pack_start(dashboard_btn, False, False, 5)
        menu_box.pack_start(maintenance_btn, False, False, 5)
        menu_box.pack_start(inventory_btn, False, False, 5)
        menu_box.pack_start(reports_btn, False, False, 5)
        
        header_bar.pack_start(menu_box)
        
        # Notebook for main content
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        main_vbox.pack_start(self.notebook, True, True, 0)
        
        # Create application tabs
        self._create_dashboard_tab()
        self._create_maintenance_tab()
        self._create_inventory_tab()
        self._create_reports_tab()
    
    def _create_dashboard_tab(self):
        """Create the main engineering dashboard"""
        self.dashboard = MarineEngineeringDashboard(self.engineering_service)
        self.notebook.append_page(self.dashboard, Gtk.Label(label="Dashboard"))
    
    def _create_maintenance_tab(self):
        """Create maintenance management tab"""
        maintenance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        maintenance_box.set_margin_start(10)
        maintenance_box.set_margin_end(10)
        maintenance_box.set_margin_top(10)
        maintenance_box.set_margin_bottom(10)
        
        # TODO: Implement comprehensive maintenance interface
        label = Gtk.Label(label="Maintenance Management - Under Development")
        maintenance_box.pack_start(label, True, True, 0)
        
        self.notebook.append_page(maintenance_box, Gtk.Label(label="Maintenance"))
    
    def _create_inventory_tab(self):
        """Create inventory management tab"""
        inventory_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inventory_box.set_margin_start(10)
        inventory_box.set_margin_end(10)
        inventory_box.set_margin_top(10)
        inventory_box.set_margin_bottom(10)
        
        # TODO: Implement marine parts inventory
        label = Gtk.Label(label="Marine Parts Inventory - Under Development")
        inventory_box.pack_start(label, True, True, 0)
        
        self.notebook.append_page(inventory_box, Gtk.Label(label="Inventory"))
    
    def _create_reports_tab(self):
        """Create engineering reports tab"""
        reports_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        reports_box.set_margin_start(10)
        reports_box.set_margin_end(10)
        reports_box.set_margin_top(10)
        reports_box.set_margin_bottom(10)
        
        # TODO: Implement professional reporting
        label = Gtk.Label(label="Engineering Reports - Under Development")
        reports_box.pack_start(label, True, True, 0)
        
        self.notebook.append_page(reports_box, Gtk.Label(label="Reports"))
    
    def _apply_professional_styling(self):
        """Apply professional marine engineering styling"""
        css_provider = Gtk.CssProvider()
        css = """
        .critical-alert {
            background-color: #ff6b6b;
            color: white;
            padding: 5px;
            border-radius: 3px;
        }
        .warning-alert {
            background-color: #ffd93d;
            color: black;
            padding: 5px;
            border-radius: 3px;
        }
        .normal-status {
            background-color: #6bcf7f;
            color: white;
            padding: 5px;
            border-radius: 3px;
        }
        """
        css_provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    
    def _initialize_async_operations(self):
        """Initialize background operations"""
        # Start background health monitoring
        GLib.timeout_add_seconds(60, self._background_health_check)
    
    def _background_health_check(self):
        """Background component health monitoring"""
        try:
            # TODO: Implement actual background health checks
            logger.info("Performing background health checks...")
        except Exception as e:
            logger.error(f"Background health check failed: {e}")
        return True  # Continue periodic checks
    
    def _show_dashboard(self, button):
        self.notebook.set_current_page(0)
    
    def _show_maintenance(self, button):
        self.notebook.set_current_page(1)
    
    def _show_inventory(self, button):
        self.notebook.set_current_page(2)
    
    def _show_reports(self, button):
        self.notebook.set_current_page(3)

# --- Application Entry Point ---
def main():
    # Create necessary directories
    Path('reports').mkdir(exist_ok=True)
    Path('backups').mkdir(exist_ok=True)
    Path('attachments').mkdir(exist_ok=True)
    
    # Initialize and run application
    app = MarineEngineerPro()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    
    logger.info("Marine Engineer Pro started successfully")
    Gtk.main()

if __name__ == "__main__":
    main()