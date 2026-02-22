"""
Rule Engine - Drives all SEO checks from JSON rule definitions.

Design:
- Rules are loaded from JSON files at startup
- Rules declare conditions as expression trees
- Engine evaluates conditions against page/site data
- Results are scored and classified by severity
- New rules added without code changes
"""

from __future__ import annotations

import json
import operator
import re
from pathlib import Path
from typing import Any, Callable

import structlog
from pydantic import BaseModel, Field, field_validator

from app.engines.base import Issue, IssueCategory, Severity

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────
# Rule Schema
# ─────────────────────────────────────────────

class RuleCondition(BaseModel):
    """A single condition to evaluate against page data."""
    field: str           # Dot-notation path: "meta.title", "status_code", etc.
    operator: str        # eq, ne, lt, gt, lte, gte, contains, not_contains, matches, exists, not_exists
    value: Any = None    # Expected value (None for exists/not_exists)
    transform: str | None = None  # len, lower, upper, strip, count


class Rule(BaseModel):
    """
    Complete rule definition loaded from JSON.
    Rules are the atomic unit of SEO check logic.
    """
    id: str
    name: str
    description: str
    category: IssueCategory
    severity: Severity
    conditions: list[RuleCondition]
    condition_logic: str = "AND"  # AND | OR
    impact_score: float = Field(ge=0.0, le=100.0, default=50.0)
    effort_score: float = Field(ge=1.0, le=10.0, default=5.0)
    recommendation: str = ""
    documentation_url: str = ""
    enabled: bool = True
    applies_to: str = "page"     # page | site | all
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_-]{2,63}$", v):
            raise ValueError(f"Rule ID '{v}' must be lowercase alphanumeric with hyphens/underscores")
        return v


class RuleEvaluationResult(BaseModel):
    rule: Rule
    passed: bool
    affected_urls: list[str] = Field(default_factory=list)
    affected_count: int = 0
    context: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────
# Operator Registry
# ─────────────────────────────────────────────

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "gt": operator.gt,
    "lte": operator.le,
    "gte": operator.ge,
    "contains": lambda a, b: b in a if a else False,
    "not_contains": lambda a, b: b not in a if a else True,
    "matches": lambda a, b: bool(re.search(b, str(a))) if a else False,
    "not_matches": lambda a, b: not bool(re.search(b, str(a))) if a else True,
    "exists": lambda a, _: a is not None and a != "" and a != [],
    "not_exists": lambda a, _: a is None or a == "" or a == [],
    "in": lambda a, b: a in b if b else False,
    "not_in": lambda a, b: a not in b if b else True,
    "length_lt": lambda a, b: len(a) < b if a else True,
    "length_gt": lambda a, b: len(a) > b if a else False,
    "length_eq": lambda a, b: len(a) == b if a else False,
    "starts_with": lambda a, b: str(a).startswith(b) if a else False,
    "ends_with": lambda a, b: str(a).endswith(b) if a else False,
}

TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "len": lambda x: len(x) if x else 0,
    "lower": lambda x: x.lower() if isinstance(x, str) else x,
    "upper": lambda x: x.upper() if isinstance(x, str) else x,
    "strip": lambda x: x.strip() if isinstance(x, str) else x,
    "count": lambda x: len(x) if hasattr(x, "__len__") else 0,
    "bool": bool,
    "int": lambda x: int(x) if x else 0,
    "float": lambda x: float(x) if x else 0.0,
}


# ─────────────────────────────────────────────
# Data Accessor
# ─────────────────────────────────────────────

def get_nested_value(data: dict[str, Any], path: str) -> Any:
    """
    Extract a value from nested dict using dot notation.
    Example: get_nested_value(page, "meta.title") -> "My Title"
    """
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            try:
                current = current[int(key)]
            except IndexError:
                return None
        else:
            return None
    return current


def apply_transform(value: Any, transform: str | None) -> Any:
    """Apply optional transformation to extracted value."""
    if transform is None:
        return value
    fn = TRANSFORMS.get(transform)
    if fn is None:
        logger.warning("Unknown transform", transform=transform)
        return value
    try:
        return fn(value)
    except Exception:
        return value


# ─────────────────────────────────────────────
# Rule Evaluator
# ─────────────────────────────────────────────

class RuleEvaluator:
    """Evaluates a single rule against page data."""

    def evaluate_condition(
        self,
        condition: RuleCondition,
        page_data: dict[str, Any],
    ) -> bool:
        """Evaluate a single condition against page data."""
        raw_value = get_nested_value(page_data, condition.field)
        value = apply_transform(raw_value, condition.transform)

        op_fn = OPERATORS.get(condition.operator)
        if op_fn is None:
            logger.warning("Unknown operator", operator=condition.operator)
            return False

        try:
            return op_fn(value, condition.value)
        except (TypeError, AttributeError) as e:
            logger.debug(
                "Condition evaluation error",
                field=condition.field,
                operator=condition.operator,
                value=value,
                error=str(e),
            )
            return False

    def evaluate_rule(
        self,
        rule: Rule,
        page_data: dict[str, Any],
    ) -> bool:
        """
        Evaluate all conditions of a rule.
        Returns True if the rule FAILS (i.e., issue is detected).
        """
        results = [
            self.evaluate_condition(cond, page_data)
            for cond in rule.conditions
        ]

        if rule.condition_logic == "AND":
            return all(results)
        elif rule.condition_logic == "OR":
            return any(results)
        return False


# ─────────────────────────────────────────────
# Rule Registry
# ─────────────────────────────────────────────

class RuleRegistry:
    """
    Loads and manages all rule definitions.
    Rules are loaded from JSON files organized by category.
    """

    def __init__(self, rules_dir: Path):
        self.rules_dir = rules_dir
        self._rules: dict[str, Rule] = {}
        self._loaded = False

    def load(self) -> None:
        """Load all rule JSON files from the rules directory."""
        count = 0
        for json_file in self.rules_dir.glob("**/*.json"):
            try:
                with open(json_file) as f:
                    data = json.load(f)

                rules_data = data if isinstance(data, list) else [data]
                for rule_data in rules_data:
                    rule = Rule.model_validate(rule_data)
                    if rule.enabled:
                        self._rules[rule.id] = rule
                        count += 1

            except Exception as e:
                logger.error("Failed to load rule file", file=str(json_file), error=str(e))

        self._loaded = True
        logger.info("Rules loaded", total=count, files=len(list(self.rules_dir.glob("**/*.json"))))

    def get_by_category(self, category: IssueCategory) -> list[Rule]:
        return [r for r in self._rules.values() if r.category == category]

    def get_by_id(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def get_all(self) -> list[Rule]:
        return list(self._rules.values())

    @property
    def loaded(self) -> bool:
        return self._loaded


# ─────────────────────────────────────────────
# Score Calculator
# ─────────────────────────────────────────────

SEVERITY_WEIGHTS = {
    Severity.CRITICAL: 25.0,
    Severity.HIGH: 15.0,
    Severity.MEDIUM: 8.0,
    Severity.LOW: 3.0,
    Severity.INFO: 0.0,
}


def calculate_category_score(
    issues: list[Issue],
    total_checks: int,
    pages_analyzed: int = 1,
) -> float:
    """
    Calculate a category score from 0-100.

    Formula:
    - Start at 100
    - Deduct points based on severity × (affected_pages / total_pages) ratio
    - Normalize to ensure score doesn't go below 0
    """
    if total_checks == 0:
        return 100.0

    penalty = 0.0
    for issue in issues:
        weight = SEVERITY_WEIGHTS.get(issue.severity, 0.0)
        coverage = min(1.0, issue.affected_count / max(1, pages_analyzed))
        issue_penalty = weight * (0.5 + 0.5 * coverage)
        penalty += issue_penalty

    # Normalize: max possible penalty if ALL checks fail
    max_penalty = sum(SEVERITY_WEIGHTS.values()) * min(total_checks, 10)
    score = max(0.0, 100.0 - (penalty / max(max_penalty, 1)) * 100.0)
    return round(score, 2)


# ─────────────────────────────────────────────
# Impact Score Formula
# ─────────────────────────────────────────────

def calculate_impact_score(
    severity: Severity,
    affected_count: int,
    total_pages: int,
    rule_impact_score: float,
) -> float:
    """
    Calculate an issue's impact score (0-100).

    Impact = Rule Base Score × Severity Multiplier × Coverage Ratio

    This tells us: "How much is this issue hurting us?"
    """
    severity_multipliers = {
        Severity.CRITICAL: 1.0,
        Severity.HIGH: 0.75,
        Severity.MEDIUM: 0.50,
        Severity.LOW: 0.25,
        Severity.INFO: 0.0,
    }

    multiplier = severity_multipliers.get(severity, 0.5)
    coverage = min(1.0, affected_count / max(1, total_pages))
    impact = rule_impact_score * multiplier * (0.3 + 0.7 * coverage)
    return round(min(100.0, impact), 2)


# ─────────────────────────────────────────────
# Global Registry Instance
# ─────────────────────────────────────────────

_registry: RuleRegistry | None = None


def get_rule_registry() -> RuleRegistry:
    global _registry
    if _registry is None:
        rules_dir = Path(__file__).parent.parent / "rules" / "definitions"
        _registry = RuleRegistry(rules_dir)
        _registry.load()
    return _registry
