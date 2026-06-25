"""Pytest bootstrap — must run before any test module imports app.config."""

import os

os.environ.setdefault("ARGUS_TESTING", "1")
os.environ.setdefault("ARGUS_VISION_BACKEND", "mock")