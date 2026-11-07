"""Compatibility import for common.records."""

import sys
from common import records as _implementation
sys.modules[__name__] = _implementation
