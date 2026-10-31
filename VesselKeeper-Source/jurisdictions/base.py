"""Base classes for jurisdiction rulesets.
All validations are non-blocking and advisory only.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum
import datetime


class WarningSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class RuleSetWarning:
    """Non-blocking advisory warning from jurisdiction ruleset."""
    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.INFO
    entity_type: str = ""
    entity_id: Optional[int] = None
    field: str = ""
    suggestion: str = ""
    authority: str = ""
    
    def to_dict(self):
        return {
            'code': self.code,
            'message': self.message,
            'severity': self.severity.value,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'field': self.field,
            'suggestion': self.suggestion,
            'authority': self.authority,
        }


@dataclass
class RuleSetChecklistItem:
    """Advisory checklist item for compliance tracking."""
    code: str
    title: str
    description: str
    required: bool = False
    category: str = "general"
    entity_type: str = ""
    authority: str = ""
    due_days: Optional[int] = None
    completed: bool = False
    
    def to_dict(self):
        return {
            'code': self.code,
            'title': self.title,
            'description': self.description,
            'required': self.required,
            'category': self.category,
            'entity_type': self.entity_type,
            'authority': self.authority,
            'due_days': self.due_days,
            'completed': self.completed,
        }


@dataclass
class RuleSet:
    """Base jurisdiction ruleset. All validations are advisory."""
    id: str = "international"
    name: str = "International/IMO"
    country_code: str = "INT"
    caricom_member: bool = False
    locale: str = "en_US"
    currency: str = "USD"
    date_format: str = "YYYY-MM-DD"
    timezone: str = "UTC"
    authorities: List[Dict[str, str]] = field(default_factory=list)
    default_taxes: List[Dict[str, Any]] = field(default_factory=list)
    expense_categories: List[str] = field(default_factory=list)
    vessel_required_fields: List[str] = field(default_factory=list)
    vessel_fields: List[Dict[str, Any]] = field(default_factory=list)
    crew_cert_presets: List[Dict[str, Any]] = field(default_factory=list)
    report_templates: Dict[str, Any] = field(default_factory=dict)
    retention_guidance: Dict[str, Any] = field(default_factory=dict)
    ports_of_registry: List[str] = field(default_factory=list)
    common_ports: List[str] = field(default_factory=list)
    disclaimer: str = "This application provides informational guard rails only. It is not a substitute for legal advice or current legislation/regulations. Always verify with the applicable authority."
    ruleset_version: str = "1.0.0"
    updated_at: str = ""
    
    def get_defaults(self) -> Dict[str, Any]:
        """Get configuration defaults for this ruleset."""
        default_tax = None
        for tax in self.default_taxes:
            if default_tax is None or tax.get('default', False):
                default_tax = tax
                if tax.get('default', False):
                    break
        tax_rate = float(default_tax.get('rate', 0.0)) if default_tax else 0.0
        return {
            'country': self.country_code,
            'locale': self.locale,
            'timezone': self.timezone,
            'currency': self.currency,
            'date_format': self.date_format,
            'tax_regime': default_tax.get('name', 'None') if default_tax else 'None',
            'tax_rate': tax_rate,
            'jurisdiction_preset': self.id,
            'jurisdiction_ruleset_id': self.id,
            'jurisdiction_ruleset_version': self.ruleset_version,
            'jurisdiction_ruleset_updated_at': self.updated_at,
        }
    
    def validate(self, ctx: Dict[str, Any], entity_type: str, entity: Any) -> List[RuleSetWarning]:
        """Validate entity against jurisdiction rules - returns non-blocking warnings."""
        warnings = []
        return warnings
    
    def get_checklist(self, ctx: Dict[str, Any], entity_type: str) -> List[RuleSetChecklistItem]:
        """Get advisory checklist items for entity type."""
        return []
    
    def get_expense_categories(self) -> List[str]:
        """Get suggested expense categories."""
        return self.expense_categories
    
    def get_crew_cert_presets(self) -> List[Dict[str, Any]]:
        """Get suggested crew certifications."""
        return self.crew_cert_presets
    
    def get_report_templates(self) -> Dict[str, Any]:
        """Get regulatory report templates."""
        return self.report_templates
