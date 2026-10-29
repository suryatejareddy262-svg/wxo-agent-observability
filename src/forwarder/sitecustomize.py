"""
forwarder/sitecustomize.py
==========================
Auto-loaded by Python at startup when this directory is in PYTHONPATH.
Initializes WXO tracing hook for agent frameworks and SDKs.
"""

import sys
import os

try:
    import forwarder.hook
    forwarder.hook.instrument()
except Exception:
    pass
