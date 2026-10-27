"""VPN-only Citespec membership portal.

The package owns local accounts, TOTP, sessions, registration intake, and
admin-only membership views.  It never registers shell, VS Code, file-transfer,
or GPU-allocation routes; those remain CLI/SSH capabilities.
"""

from __future__ import annotations
