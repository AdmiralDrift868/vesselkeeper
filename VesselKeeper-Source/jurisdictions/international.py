"""Generic international / IMO fallback ruleset.

Used when no CARICOM jurisdiction is active or when a ruleset fails to load.
It deliberately makes no national tax assumptions.
"""

from .base import RuleSet, RuleSetWarning, WarningSeverity
from .caricom_data import (
    COMMON_CERT_PRESETS,
    COMMON_EXPENSE_CATEGORIES,
    COMMON_REPORTS,
    COMMON_RETENTION,
    DISCLAIMER,
)


class InternationalRuleSet(RuleSet):
    def __init__(self):
        super().__init__(
            id="international",
            name="International / IMO",
            country_code="INT",
            caricom_member=False,
            locale="en_US",
            currency="USD",
            date_format="YYYY-MM-DD",
            timezone="UTC",
            authorities=[
                {"name": "International Maritime Organization (IMO)",
                 "role": "International maritime standards"},
            ],
            default_taxes=[],
            expense_categories=list(COMMON_EXPENSE_CATEGORIES),
            vessel_required_fields=["flag_state", "official_number"],
            vessel_fields=[
                {"key": "flag_state", "label": "Flag State", "type": "combo_entry", "required": True},
                {"key": "port_of_registry", "label": "Port of Registry", "type": "combo_entry",
                 "required": False},
                {"key": "official_number", "label": "Official Number", "type": "str", "required": True},
                {"key": "imo_number", "label": "IMO Number", "type": "str", "required": False},
                {"key": "registration_expiry", "label": "Registration Expiry", "type": "date",
                 "required": False},
            ],
            crew_cert_presets=[dict(p) for p in COMMON_CERT_PRESETS],
            report_templates={r["id"]: dict(r) for r in COMMON_REPORTS},
            retention_guidance=dict(COMMON_RETENTION),
            ports_of_registry=[],
            common_ports=[],
            disclaimer=DISCLAIMER,
            ruleset_version="1.0.0",
            updated_at="2026-10-27",
        )
        self.tax_name = "Tax"
        self.tax_rate = 0.0
        self.tax_authority = ""
        self.caricom_status = "none"

    def get_defaults(self):
        defaults = super().get_defaults()
        defaults.update({
            'tax_regime': self.tax_name,
            'tax_rate': self.tax_rate,
            'country': self.country_code,
            'locale': self.locale,
            'timezone': self.timezone,
            'date_format': self.date_format,
            'currency': self.currency,
            'jurisdiction_preset': self.id,
            'jurisdiction_ruleset_id': self.id,
            'jurisdiction_ruleset_version': self.ruleset_version,
            'jurisdiction_ruleset_updated_at': self.updated_at,
        })
        return defaults

    def validate(self, ctx, entity_type, entity):
        warnings = []
        if entity_type == "vessel":
            for key, label in (("flag_state", "Flag State"), ("official_number", "Official Number")):
                value = entity.get(key) if isinstance(entity, dict) else getattr(entity, key, "")
                if not str(value or "").strip():
                    warnings.append(RuleSetWarning(
                        code="vessel_missing_%s" % key,
                        message="%s is not recorded for this vessel." % label,
                        severity=WarningSeverity.WARNING,
                        entity_type="vessel", field=key,
                        suggestion="Record it under Vessel details.",
                    ))
        return warnings

    def get_checklist(self, ctx, entity_type="general"):
        return []

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "country_code": self.country_code,
            "caricom_status": "none", "locale": self.locale, "currency": self.currency,
            "date_format": self.date_format, "timezone": self.timezone,
            "tax_regime": "None", "tax_rate": 0.0, "tax_authority": "",
            "ruleset_version": self.ruleset_version, "updated_at": self.updated_at,
            "authorities": list(self.authorities), "disclaimer": self.disclaimer,
            "is_custom": False,
        }
