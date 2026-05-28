"""
Privacy Scanner — Layer 7 of the 10-layer pipeline.

Scans text for PII, secrets, and sensitive content before
it reaches any AI provider. Applies sensitivity labels and
generates redaction maps for the REDACTED_ONLINE privacy mode.

Detection categories:
  - Email addresses
  - Phone numbers
  - API keys / tokens (generic patterns)
  - IP addresses
  - Credit card numbers
  - Names (basic heuristic)
  - URLs with credentials
"""

import re
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

from core.models import SensitivityLevel


@dataclass
class PIIMatch:
    """A single PII detection."""
    category: str           # "email", "phone", "api_key", etc.
    value: str              # The detected text
    start: int              # Start position in original text
    end: int                # End position
    confidence: float       # 0.0–1.0


@dataclass
class ScanResult:
    """Result of scanning a text for PII/secrets."""
    original_text: str
    detections: List[PIIMatch] = field(default_factory=list)
    suggested_sensitivity: SensitivityLevel = SensitivityLevel.PUBLIC
    redacted_text: Optional[str] = None

    @property
    def has_pii(self) -> bool:
        return len(self.detections) > 0

    @property
    def categories_found(self) -> List[str]:
        return list(set(d.category for d in self.detections))

    def to_dict(self) -> Dict:
        return {
            "has_pii": self.has_pii,
            "detection_count": len(self.detections),
            "categories": self.categories_found,
            "suggested_sensitivity": self.suggested_sensitivity.value,
        }


# ─── Detection Patterns ─────────────────────────────────────────

_PATTERNS: List[Tuple[str, str, float]] = [
    # (category, regex_pattern, confidence)

    # Email
    ("email", r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', 0.95),

    # Phone numbers (international and US formats)
    ("phone", r'(?:\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', 0.80),

    # API Keys / Tokens (generic patterns)
    ("api_key", r'(?:sk|pk|api|token|key|secret|bearer)[-_]?[a-zA-Z0-9]{20,}', 0.90),
    ("api_key", r'(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}', 0.95),  # AWS access keys
    ("api_key", r'ghp_[a-zA-Z0-9]{36}', 0.95),  # GitHub PAT
    ("api_key", r'sk-[a-zA-Z0-9]{32,}', 0.95),  # OpenAI / Anthropic keys
    ("api_key", r'xox[baprs]-[a-zA-Z0-9-]+', 0.90),  # Slack tokens

    # IP addresses
    ("ip_address", r'\b(?:\d{1,3}\.){3}\d{1,3}\b', 0.70),

    # Credit card numbers (basic: 13-19 digit sequences)
    ("credit_card", r'\b(?:\d{4}[-\s]?){3,4}\d{1,4}\b', 0.60),

    # SSN (US format)
    ("ssn", r'\b\d{3}-\d{2}-\d{4}\b', 0.85),

    # URLs with embedded credentials
    ("credential_url", r'https?://[^:]+:[^@]+@[^\s]+', 0.95),

    # Private key blocks
    ("private_key", r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----', 0.99),

    # Password assignments
    ("password", r'(?:password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{4,}', 0.85),
]

# Categories that indicate RESTRICTED sensitivity
_RESTRICTED_CATEGORIES = {"private_key", "ssn", "credit_card"}

# Categories that indicate CONFIDENTIAL sensitivity
_CONFIDENTIAL_CATEGORIES = {"api_key", "password", "credential_url"}

# Categories that indicate at least INTERNAL
_INTERNAL_CATEGORIES = {"email", "phone", "ip_address"}


class PrivacyScanner:
    """Scans text for PII, secrets, and sensitive content."""

    def scan(self, text: str) -> ScanResult:
        """Scan text and return all PII detections with sensitivity level."""
        detections: List[PIIMatch] = []

        for category, pattern, confidence in _PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                detections.append(PIIMatch(
                    category=category,
                    value=match.group(),
                    start=match.start(),
                    end=match.end(),
                    confidence=confidence,
                ))

        # Determine sensitivity level based on what was found
        categories = set(d.category for d in detections)
        if categories & _RESTRICTED_CATEGORIES:
            sensitivity = SensitivityLevel.RESTRICTED
        elif categories & _CONFIDENTIAL_CATEGORIES:
            sensitivity = SensitivityLevel.CONFIDENTIAL
        elif categories & _INTERNAL_CATEGORIES:
            sensitivity = SensitivityLevel.INTERNAL
        else:
            sensitivity = SensitivityLevel.PUBLIC

        result = ScanResult(
            original_text=text,
            detections=detections,
            suggested_sensitivity=sensitivity,
        )

        # Generate redacted version
        if detections:
            result.redacted_text = self._redact(text, detections)

        return result

    def scan_batch(self, texts: List[str]) -> List[ScanResult]:
        """Scan multiple texts."""
        return [self.scan(t) for t in texts]

    def _redact(self, text: str, detections: List[PIIMatch]) -> str:
        """Replace PII with category labels."""
        # Sort by position (reverse) to replace from end to start
        sorted_dets = sorted(detections, key=lambda d: d.start, reverse=True)
        redacted = text
        for det in sorted_dets:
            replacement = f"[{det.category.upper()}_REDACTED]"
            redacted = redacted[:det.start] + replacement + redacted[det.end:]
        return redacted

    def get_redaction_map(self, result: ScanResult) -> List[Dict]:
        """Get a detailed redaction map for audit logging."""
        return [
            {
                "category": d.category,
                "original_length": len(d.value),
                "start": d.start,
                "end": d.end,
                "confidence": d.confidence,
            }
            for d in result.detections
        ]
