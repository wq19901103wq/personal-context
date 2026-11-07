"""Compatibility import for common.history."""

import sys
from common import history as _implementation
sys.modules[__name__] = _implementation
