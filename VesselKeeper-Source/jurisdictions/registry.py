"""Ruleset registry: built-in CARICOM nations plus optional user JSON files.

Custom rulesets are data-only. They are schema-checked and never executed as
code. A malformed custom file is ignored with a logged warning so it can never
prevent the application from starting.
"""

import json
import logging
import os
import re
from pathlib import Path

from .caricom import CaricomRuleSet
from .caricom_data import NATIONS
from .international import InternationalRuleSet

logger = logging.getLogger("vessel_keeper.jurisdictions")

DEFAULT_JURISDICTION = "tt"
BUILTIN_IDS = {"international"} | {data["id"] for data in NATIONS.values()}

REQUIRED_STRINGS = ("id", "name", "country_code", "locale", "currency",
                    "date_format", "timezone")
MAX_FILE_BYTES = 64 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,31}$")
ALLOWED_REPORT_DATASETS = {
    "vessels", "inventory", "tasks", "trips", "suppliers", "stock_transactions",
    "expenses", "budgets", "fuel_logs", "crew", "certifications", "training",
    "watch_schedules", "task_templates", "task_parts",
}
ALLOWED_DATE_FORMATS = {
    "DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD", "DD-MM-YYYY",
    "YYYY/MM/DD", "DD.MM.YYYY",
}


class RulesetError(ValueError):
    pass


def default_custom_dir():
    override = os.environ.get("VESSEL_KEEPER_JURISDICTIONS")
    if override:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(override)))
    return os.path.join(os.path.expanduser("~"), ".config", "vessel_keeper",
                        "jurisdictions")


def _require_string(data, key, maxlen=160):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RulesetError("%s must be a non-empty string" % key)
    if len(value) > maxlen:
        raise RulesetError("%s is too long (max %d characters)" % (key, maxlen))
    return value.strip()


def _string_list(data, key, max_items=100, maxlen=120):
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise RulesetError("%s must be a list" % key)
    if len(value) > max_items:
        raise RulesetError("%s has too many entries (max %d)" % (key, max_items))
    out = []
    for item in value:
        if not isinstance(item, str) or len(item) > maxlen:
            raise RulesetError("%s entries must be strings up to %d characters" % (key, maxlen))
        if item.strip():
            out.append(item.strip())
    return out


def validate_custom_ruleset(data):
    """Validate a user-supplied ruleset dict. Raises RulesetError."""
    if not isinstance(data, dict):
        raise RulesetError("Ruleset file must contain a JSON object")
    cleaned = {}
    for key in REQUIRED_STRINGS:
        cleaned[key] = _require_string(data, key)
    if not ID_RE.match(cleaned["id"]):
        raise RulesetError("id must match %s" % ID_RE.pattern)
    if cleaned["id"] in BUILTIN_IDS:
        raise RulesetError("id %r is reserved for a built-in ruleset" % cleaned["id"])
    if len(cleaned["country_code"]) > 8:
        raise RulesetError("country_code is too long")
    if cleaned["date_format"] not in ALLOWED_DATE_FORMATS:
        raise RulesetError("date_format must be one of: %s"
                           % ", ".join(sorted(ALLOWED_DATE_FORMATS)))
    if len(cleaned["currency"]) != 3 or not cleaned["currency"].isalpha():
        raise RulesetError("currency must be a three-letter ISO code")
    cleaned["currency"] = cleaned["currency"].upper()

    authorities = data.get("authorities", [])
    if not isinstance(authorities, list) or len(authorities) > 20:
        raise RulesetError("authorities must be a list of up to 20 entries")
    clean_authorities = []
    for entry in authorities:
        if not isinstance(entry, dict):
            raise RulesetError("each authority must be an object")
        name = _require_string(entry, "name")
        role = entry.get("role", "")
        if not isinstance(role, str) or len(role) > 200:
            raise RulesetError("authority role must be a string up to 200 characters")
        clean_authorities.append({"name": name, "role": role.strip()})
    cleaned["authorities"] = clean_authorities

    taxes = data.get("taxes")
    # Accept the convenience short form used in the documentation and in
    # hand-written rulesets: a single tax_name/tax_rate pair.
    if taxes is None and ("tax_name" in data or "tax_rate" in data):
        taxes = [{
            "name": data.get("tax_name") or "Tax",
            "rate": data.get("tax_rate", 0.0),
            "default": True,
        }]
    if taxes is None:
        taxes = []
    if not isinstance(taxes, list) or len(taxes) > 10:
        raise RulesetError("taxes must be a list of up to 10 entries")
    clean_taxes = []
    for entry in taxes:
        if not isinstance(entry, dict):
            raise RulesetError("each tax must be an object")
        name = _require_string(entry, "name", 60)
        rate = entry.get("rate", 0.0)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise RulesetError("tax rate must be a number")
        if not (0.0 <= float(rate) <= 100.0):
            raise RulesetError("tax rate must be between 0 and 100")
        note = entry.get("note", "")
        if not isinstance(note, str) or len(note) > 300:
            raise RulesetError("tax note must be a string up to 300 characters")
        clean_taxes.append({"name": name, "rate": float(rate),
                            "default": bool(entry.get("default", False)),
                            "note": note.strip()})
    cleaned["taxes"] = clean_taxes

    certs = data.get("cert_extras", [])
    if not isinstance(certs, list) or len(certs) > 50:
        raise RulesetError("cert_extras must be a list of up to 50 entries")
    clean_certs = []
    for entry in certs:
        if not isinstance(entry, dict):
            raise RulesetError("each certificate preset must be an object")
        name = _require_string(entry, "name", 160)
        body = entry.get("issuing_body", "")
        category = entry.get("category", "Other")
        if not isinstance(body, str) or not isinstance(category, str):
            raise RulesetError("certificate issuing_body and category must be strings")
        clean_certs.append({"name": name, "issuing_body": body.strip(),
                            "category": category.strip() or "Other"})
    cleaned["cert_extras"] = clean_certs

    reports = data.get("reports", [])
    if not isinstance(reports, list) or len(reports) > 20:
        raise RulesetError("reports must be a list of up to 20 entries")
    clean_reports = []
    for entry in reports:
        if not isinstance(entry, dict):
            raise RulesetError("each report must be an object")
        rid = _require_string(entry, "id", 32)
        if not ID_RE.match(rid):
            raise RulesetError("report id %r is invalid" % rid)
        dataset = _require_string(entry, "dataset", 40)
        if dataset not in ALLOWED_REPORT_DATASETS:
            raise RulesetError("report %r uses an unknown dataset %r" % (rid, dataset))
        clean_reports.append({
            "id": rid,
            "title": _require_string(entry, "title", 120),
            "dataset": dataset,
            "notes": str(entry.get("notes", ""))[:300],
        })
    cleaned["reports"] = clean_reports

    for key in ("expense_extras", "ports_of_registry", "common_ports", "vessel_extras"):
        cleaned[key] = _string_list(data, key)
    tax_authority = data.get("tax_authority", "")
    if tax_authority and (not isinstance(tax_authority, str) or len(tax_authority) > 160):
        raise RulesetError("tax_authority must be a string up to 160 characters")
    cleaned["tax_authority"] = tax_authority.strip()
    status = data.get("caricom_status", "custom")
    if status not in ("full", "associate", "custom", "none", "international"):
        raise RulesetError("caricom_status must be full, associate, custom, none or international")
    cleaned["caricom_status"] = status
    disclaimer = data.get("disclaimer", "")
    if disclaimer and (not isinstance(disclaimer, str) or len(disclaimer) > 2000):
        raise RulesetError("disclaimer must be a string up to 2000 characters")
    cleaned["disclaimer"] = disclaimer.strip()
    version = data.get("ruleset_version", "1.0.0")
    if not isinstance(version, str) or len(version) > 32:
        raise RulesetError("ruleset_version must be a short string")
    cleaned["ruleset_version"] = version
    updated = data.get("updated", "")
    if updated and (not isinstance(updated, str) or len(updated) > 32):
        raise RulesetError("updated must be a short string")
    cleaned["updated"] = updated
    cleaned["_custom"] = True
    return cleaned


class JurisdictionRegistry:
    def __init__(self, custom_dir=None, allow_custom=True):
        self.custom_dir = custom_dir if custom_dir is not None else default_custom_dir()
        self._allow_custom = allow_custom
        self._rulesets = {}
        self.errors = []
        self.reload()

    def reload(self, allow_custom=None):
        if allow_custom is not None:
            self._allow_custom = allow_custom
        self._rulesets = {"international": InternationalRuleSet()}
        for data in NATIONS.values():
            ruleset = CaricomRuleSet(data)
            self._rulesets[ruleset.id] = ruleset
        self.errors = []
        if self._allow_custom:
            self._load_custom()
        return self

    def _load_custom(self):
        root = Path(self.custom_dir)
        if not root.exists():
            return
        try:
            candidates = sorted(root.glob("*.json"))
        except OSError as exc:
            self.errors.append("Could not list %s: %s" % (root, exc))
            logger.warning("Could not list custom rulesets: %s", exc)
            return
        for path in candidates:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise RulesetError("file is larger than %d bytes" % MAX_FILE_BYTES)
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                cleaned = validate_custom_ruleset(data)
                ruleset = CaricomRuleSet(cleaned)
                ruleset.is_custom = True
                self._rulesets[ruleset.id] = ruleset
                logger.info("Loaded custom jurisdiction ruleset %s from %s",
                            ruleset.id, path)
            except (OSError, json.JSONDecodeError, RulesetError) as exc:
                message = "Ignored custom ruleset %s: %s" % (path.name, exc)
                self.errors.append(message)
                logger.warning(message)

    def get(self, ruleset_id):
        return self._rulesets.get(str(ruleset_id or "").strip().lower())

    def require(self, ruleset_id):
        return self.get(ruleset_id) or self.get(DEFAULT_JURISDICTION) or self.get("international")

    def default(self):
        return self.require(DEFAULT_JURISDICTION)

    def list(self):
        def sort_key(rs):
            status = getattr(rs, "caricom_status", "none")
            rank = {"full": 0, "associate": 1}.get(status, 2)
            if rs.id == DEFAULT_JURISDICTION:
                rank = -1
            return (rank, rs.name.lower())
        return sorted(self._rulesets.values(), key=sort_key)

    def ids(self):
        return [rs.id for rs in self.list()]


_REGISTRY = None


def get_registry(custom_dir=None, allow_custom=True):
    global _REGISTRY
    if custom_dir is not None:
        # An explicit directory always builds an isolated registry so callers
        # never mutate the process-wide default.
        return JurisdictionRegistry(custom_dir=custom_dir,
                                    allow_custom=allow_custom)
    if _REGISTRY is None:
        _REGISTRY = JurisdictionRegistry(allow_custom=allow_custom)
    elif bool(getattr(_REGISTRY, "_allow_custom", True)) != bool(allow_custom):
        # Preferences can enable/disable custom rulesets at runtime. Reload so
        # a disabled custom file cannot keep influencing the active ruleset.
        _REGISTRY.reload(allow_custom=bool(allow_custom))
    return _REGISTRY


def reload_registry(allow_custom=True):
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = JurisdictionRegistry(allow_custom=allow_custom)
    else:
        _REGISTRY.reload(allow_custom=allow_custom)
    return _REGISTRY
