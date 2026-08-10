"""
Backward-compatibility shim. All logic has moved to ai_service.py which
tries Grok first, then Gemini. Import from there for new code.
"""
from app.services.ai_service import (
    analyze_business,
    AIError as GeminiError,
    AIQuotaError,
    AINotConfiguredError,
    BUSINESS_ANALYSIS_SCHEMA,
)

__all__ = ["analyze_business", "GeminiError", "AIQuotaError", "AINotConfiguredError", "BUSINESS_ANALYSIS_SCHEMA"]
