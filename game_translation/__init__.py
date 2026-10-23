"""Synchronous public API; the caller supplies paths and serializes project writes."""
from .engines import detect
from .project import prepare, status, load_project
from .translation import make_package, apply_results
from .build import build, verify
from .localization import inspect_localization
from .resource_inspection import inspect_resources
from .candidates import collect_candidates, locate_text
from .fonts import inspect_fonts

__all__ = ["detect", "prepare", "status", "load_project", "make_package",
           "apply_results", "build", "verify", "inspect_localization", "inspect_resources", "collect_candidates", "locate_text", "inspect_fonts"]
