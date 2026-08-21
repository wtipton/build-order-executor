"""Make the project root importable in tests (bot, catalog, schema, placement)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
