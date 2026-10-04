"""Where runs come from and where records are kept, per backend.

Everything that knows about a CI or hosting provider lives in one module here, selected by the
`backend` field: `github` now, `launchpad` (quirq's own cloud) later. Core modules never import
a backend module directly; they call `load(name)`.
"""
from __future__ import annotations

from qqresults.errors import Error

import importlib
from types import ModuleType

KNOWN = ("github", "local")


class BackendError(Error):
    pass


def load(name: str) -> ModuleType:
    if name not in KNOWN:
        raise BackendError(f"unknown backend {name!r}; known: {', '.join(KNOWN)}")
    return importlib.import_module(f"qqresults.backends.{name}")
