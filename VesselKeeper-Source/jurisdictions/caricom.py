"""CARICOM ruleset implementation with advisory guard rails.

Nothing in this module blocks a save. Existing hard validation (required
names, positive amounts, valid dates and so on) stays in vessel_keeper.py.
Jurisdiction checks return warnings that are surfaced to the user.
"""

import datetime

from .base import RuleSet, RuleSetWarning, RuleSetChecklistItem, WarningSeverity
from .caricom_data import (
    COMMON_CERT_PRESETS,
    COMMON_EXPENSE_CATEGORIES,
    COMMON_REPORTS,
    COMMON_RETENTION,
    DISCLAIMER,
    REGIONAL_EXPENSE_CATEGORIES,
    REVIEW_DATE,
)


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        key = str(item).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _field(entity, key, default=""):
    if isinstance(entity, dict):
        value = entity.get(key, default)
    else:
        value = getattr(entity, key, default)
    return default if value is None else value


def imo_number_valid(value):
    """Validate the IMO ship identification number check digit."""
    s = str(value or "").strip()
    if not s:
        return True
    if not s.isdigit() or len(s) != 7:
        return False
    digits = [int(c) for c in s]
    total = sum(d * w for d, w in zip(digits[:6], range(7, 1, -1)))
    return total % 10 == digits[6]


def _parse_iso(value):
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class CaricomRuleSet(RuleSet):
    """A ruleset built from caricom_data.NATIONS."""

    def __init__(self, data):
        taxes = list(data.get("taxes") or [])
        certs = _dedupe(
            [p["name"] for p in COMMON_CERT_PRESETS]
            + [p["name"] for p in (data.get("cert_extras") or [])]
        )
        cert_presets = []
        for preset in COMMON_CERT_PRESETS + list(data.get("cert_extras") or []):
            merged = dict(preset)
            merged.setdefault("issuing_body", "")
            cert_presets.append(merged)
        categories = _dedupe(
            COMMON_EXPENSE_CATEGORIES
            + REGIONAL_EXPENSE_CATEGORIES
            + list(data.get("expense_extras") or [])
        )
        reports = {r["id"]: dict(r) for r in COMMON_REPORTS}
        for report in data.get("reports") or []:
            reports[report["id"]] = dict(report)
        super().__init__(
            id=data["id"],
            name=data["name"],
            country_code=data["country_code"],
            caricom_member=True,
            locale=data.get("locale", "en_US"),
            currency=data.get("currency", "USD"),
            date_format=data.get("date_format", "DD/MM/YYYY"),
            timezone=data.get("timezone", "UTC"),
            authorities=list(data.get("authorities") or []),
            default_taxes=taxes,
            expense_categories=categories,
            vessel_required_fields=list(data.get("vessel_required_fields")
                                        or ["flag_state", "port_of_registry", "official_number"]),
            vessel_fields=self._vessel_fields(),
            crew_cert_presets=cert_presets,
            report_templates=reports,
            retention_guidance=dict(COMMON_RETENTION),
            ports_of_registry=list(data.get("ports_of_registry") or []),
            common_ports=list(data.get("common_ports") or []),
            disclaimer=data.get("disclaimer", DISCLAIMER),
            ruleset_version=data.get("ruleset_version", "1.0.0"),
            updated_at=data.get("updated", REVIEW_DATE),
        )
        self.caricom_status = data.get("caricom_status") or (
            "custom" if data.get("_custom") else "full")
        self.tax_authority = data.get("tax_authority", "")
        _default_tax = None
        for _tax in taxes:
            if _default_tax is None or _tax.get("default", False):
                _default_tax = _tax
                if _tax.get("default", False):
                    break
        self.tax_name = _default_tax.get("name", "Tax") if _default_tax else "Tax"
        self.tax_rate = float(_default_tax.get("rate", 0.0)) if _default_tax else 0.0
        self.tax_note = _default_tax.get("note", "") if _default_tax else ""
        self.vessel_extras = list(data.get("vessel_extras") or [])
        if self.tax_rate > 0:
            self.verify_note = (
                "Verify the current %s rate and any exemptions with %s before relying "
                "on it. This preset was last reviewed on %s."
                % (self.tax_name, self.tax_authority or "the tax authority",
                   self.updated_at)
            )
        else:
            self.verify_note = (
                "No consumption tax is preset for %s. Confirm with %s whether any "
                "consumption tax, customs duty or other charge applies. This preset "
                "was last reviewed on %s."
                % (self.name, self.tax_authority or "the tax authority",
                   self.updated_at)
            )
        self.is_custom = bool(data.get("_custom", False))

    @staticmethod
    def _vessel_fields():
        return [
            {"key": "flag_state", "label": "Flag State", "type": "combo_entry",
             "required": True},
            {"key": "port_of_registry", "label": "Port of Registry", "type": "combo_entry",
             "required": True},
            {"key": "official_number", "label": "Official Number", "type": "str",
             "required": True},
            {"key": "imo_number", "label": "IMO Number", "type": "str",
             "required": False},
            {"key": "registration_expiry", "label": "Registration Expiry", "type": "date",
             "required": False},
        ]

    def validate(self, ctx, entity_type, entity):
        warnings = []
        db = (ctx or {}).get("db")
        today = datetime.date.today()

        def add(code, message, severity=WarningSeverity.INFO, field="", suggestion=""):
            warnings.append(RuleSetWarning(
                code=code, message=message, severity=severity,
                entity_type=entity_type, entity_id=_field(entity, "id", None),
                field=field, suggestion=suggestion,
                authority=self.tax_authority
                if (entity_type == "expense" or code.startswith("tax"))
                else (self.authorities[0]["name"] if self.authorities else ""),
            ))

        if entity_type == "vessel":
            for key, label in (("flag_state", "Flag State"),
                               ("port_of_registry", "Port of Registry"),
                               ("official_number", "Official Number")):
                if not str(_field(entity, key)).strip():
                    add("vessel_missing_%s" % key,
                        "%s is not recorded for this vessel." % label,
                        WarningSeverity.WARNING, key,
                        "Record it under Vessel details for %s." % self.name)
            imo = str(_field(entity, "imo_number")).strip()
            if imo and not imo_number_valid(imo):
                add("vessel_imo_invalid",
                    "IMO number %r does not pass the IMO check-digit test." % imo,
                    WarningSeverity.WARNING, "imo_number",
                    "Confirm the seven-digit IMO number with the registry.")
            expiry = _parse_iso(_field(entity, "registration_expiry"))
            if expiry:
                days = (expiry - today).days
                if days < 0:
                    add("vessel_registration_expired",
                        "Vessel registration expired %d day(s) ago." % abs(days),
                        WarningSeverity.CRITICAL, "registration_expiry",
                        "Renew with %s." % self._authority_name())
                elif days <= 90:
                    add("vessel_registration_expiring",
                        "Vessel registration expires in %d day(s)." % days,
                        WarningSeverity.WARNING, "registration_expiry",
                        "Start the renewal with %s." % self._authority_name())
        elif entity_type == "certification":
            expiry = _parse_iso(_field(entity, "expiry_date"))
            if expiry:
                days = (expiry - today).days
                if days < 0:
                    add("cert_expired",
                        "Certification %r expired %d day(s) ago."
                        % (str(_field(entity, "name")), abs(days)),
                        WarningSeverity.CRITICAL, "expiry_date",
                        "Renew or replace this certificate before the next voyage.")
                elif days <= 90:
                    add("cert_expiring",
                        "Certification %r expires in %d day(s)."
                        % (str(_field(entity, "name")), days),
                        WarningSeverity.WARNING, "expiry_date",
                        "Schedule renewal with %s."
                        % (str(_field(entity, "issuing_body")).strip() or self._authority_name()))
            if not str(_field(entity, "cert_number")).strip():
                add("cert_no_number", "Certificate number is not recorded.",
                    WarningSeverity.INFO, "cert_number")

        elif entity_type == "crew_member":
            if not str(_field(entity, "date_joined")).strip():
                add("crew_no_join_date", "Crew member has no date joined on record.",
                    WarningSeverity.INFO, "date_joined")
            if db is not None:
                try:
                    cid = _field(entity, "id", None)
                    if cid:
                        rows = db.q("SELECT COUNT(*) c FROM certifications WHERE crew_member_id=?", (cid,))
                        if not rows or not rows[0]["c"]:
                            add("crew_no_certs",
                                "No certifications are recorded for this crew member.",
                                WarningSeverity.WARNING, "",
                                "Add STCW basic safety and any role-specific certificates.")
                except Exception:
                    pass

        elif entity_type == "expense":
            category = str(_field(entity, "category")).strip()
            if category and category not in self.expense_categories:
                add("expense_category_unlisted",
                    "Expense category %r is not in the %s recommended list."
                    % (category, self.name),
                    WarningSeverity.INFO, "category",
                    "Use a suggested category or keep the custom one for your records.")
            try:
                rate = float(_field(entity, "gst_rate", 0.0) or 0.0)
            except (TypeError, ValueError):
                rate = 0.0
            if rate > 0 and not str(_field(entity, "tax_category")).strip():
                add("expense_tax_type_missing",
                    "A %s rate of %.2f%% is applied but no tax type is selected."
                    % (self.tax_name, rate),
                    WarningSeverity.WARNING, "tax_category",
                    "Select %s so the expense can be reconciled." % self.tax_name)
            try:
                amount = float(_field(entity, "amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            if amount >= 500 and not str(_field(entity, "receipt_path")).strip():
                add("expense_no_receipt",
                    "No receipt is attached for an expense of %.2f %s."
                    % (amount, self.currency),
                    WarningSeverity.INFO, "receipt_path",
                    "Attach or reference a receipt for audit and tax purposes.")

        elif entity_type == "task":
            status = str(_field(entity, "status")).strip()
            due = _parse_iso(_field(entity, "next_due"))
            if status != "completed" and due and due < today:
                add("task_overdue",
                    "Task is overdue (due %s)." % due.isoformat(),
                    WarningSeverity.WARNING, "next_due",
                    "Complete or reschedule the task.")

        elif entity_type == "trip":
            if not str(_field(entity, "arrival")).strip():
                add("trip_no_arrival",
                    "Trip has no arrival recorded.", WarningSeverity.INFO, "arrival")
            if not str(_field(entity, "destination")).strip():
                add("trip_no_destination",
                    "Trip has no destination recorded.", WarningSeverity.INFO, "destination")

        return warnings

    def get_checklist(self, ctx, entity_type="general"):
        if self.tax_rate > 0:
            tax_title = "Verify the %s preset" % self.tax_name
        else:
            tax_title = "Confirm the tax treatment for %s" % self.name
        items = [
            RuleSetChecklistItem(
                code="verify_tax_rate",
                title=tax_title,
                description=self.verify_note,
                required=False, category="tax", authority=self.tax_authority,
            ),
            RuleSetChecklistItem(
                code="authority_check",
                title="Confirm reporting contacts with %s" % self._authority_name(),
                description="Reporting forms, fees and inspection intervals can change. "
                            "Confirm current requirements with the authority before a voyage or filing.",
                required=False, category="authority",
                authority=self._authority_name(),
            ),
            RuleSetChecklistItem(
                code="vessel_documents",
                title="Keep vessel registration and certificates current",
                description="Record flag state, port of registry, official number, IMO number "
                            "and registration expiry for every vessel in the fleet.",
                required=False, category="vessel", authority=self._authority_name(),
            ),
            RuleSetChecklistItem(
                code="crew_certificates",
                title="Maintain a crew certificate register",
                description="Record STCW basic safety and role-specific certificates with expiry dates. "
                            "The dashboard highlights expiring and expired certificates.",
                required=False, category="crew", authority=self._authority_name(),
            ),
            RuleSetChecklistItem(
                code="retention",
                title="Confirm record retention periods",
                description=self.retention_guidance.get("note", ""),
                required=False, category="records", authority=self.tax_authority,
            ),
        ]
        for idx, extra in enumerate(self.vessel_extras[:6]):
            label = str(extra or "").strip()
            if not label:
                continue
            items.append(RuleSetChecklistItem(
                code="clearance_%d" % idx,
                title=label,
                description="Confirm the current clearance, permit or notification "
                            "requirements with %s before the next voyage."
                            % self._authority_name(),
                required=False, category="clearance",
                authority=self._authority_name(),
            ))
        return items

    def _authority_name(self):
        return self.authorities[0]["name"] if self.authorities else self.name

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

    def to_dict(self):
        """Serialisable snapshot used by exports and the compliance tab."""
        return {
            "id": self.id,
            "name": self.name,
            "country_code": self.country_code,
            "caricom_status": getattr(self, "caricom_status", "full"),
            "locale": self.locale,
            "currency": self.currency,
            "date_format": self.date_format,
            "timezone": self.timezone,
            "tax_regime": self.tax_name,
            "tax_rate": self.tax_rate,
            "tax_authority": self.tax_authority,
            "ruleset_version": self.ruleset_version,
            "updated_at": self.updated_at,
            "authorities": list(self.authorities),
            "disclaimer": self.disclaimer,
            "is_custom": getattr(self, "is_custom", False),
        }
