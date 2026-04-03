"""System-level security hooks - delegates to security detectors.

This module is kept for backwards compatibility.
Use speckbot.security.detectors for the new security gateway.
"""

from speckbot.security import (
    HookResult,
    SecurityGateway,
    SecurityResult,
    create_security_gateway,
)

# Re-export for backwards compatibility
__all__ = [
    "HookResult",
    "SecurityGateway",
    "SecurityResult",
    "create_security_gateway",
]
