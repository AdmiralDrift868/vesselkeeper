"""CARICOM Jurisdiction Rulesets for Vessel Keeper.

Provides pluggable jurisdictional guard rails for Caribbean maritime
operations. All rulesets are advisory only - they are not legal advice.
"""

from .base import RuleSet, RuleSetWarning, RuleSetChecklistItem, WarningSeverity
from .caricom import CaricomRuleSet, imo_number_valid
from .international import InternationalRuleSet
from .registry import (
    DEFAULT_JURISDICTION,
    JurisdictionRegistry,
    RulesetError,
    get_registry,
    reload_registry,
    validate_custom_ruleset,
)

__all__ = [
    'RuleSet',
    'RuleSetWarning',
    'RuleSetChecklistItem',
    'WarningSeverity',
    'CaricomRuleSet',
    'InternationalRuleSet',
    'imo_number_valid',
    'JurisdictionRegistry',
    'RulesetError',
    'DEFAULT_JURISDICTION',
    'get_registry',
    'reload_registry',
    'validate_custom_ruleset',
]
