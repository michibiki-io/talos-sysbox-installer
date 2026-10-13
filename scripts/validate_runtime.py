#!/usr/bin/env python3
"""Compatibility entrypoint for the explicit operator validation runner."""
import runpy
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().parents[1]/'test/e2e/scripts/validate_runtime.py'),run_name='__main__')
