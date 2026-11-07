"""Compatibility import for common.fields."""

import sys
from common import fields as _implementation
sys.modules[__name__] = _implementation
