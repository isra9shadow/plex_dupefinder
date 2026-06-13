"""Module registry — self-registration so adding a module never edits run.py.

A module file decorates its entrypoint with ``@register("name")``; ``discover()``
imports every submodule of the ``modules`` package (recursively), which triggers
the registrations. ``run.py`` then dispatches purely by name.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable

from core.types import ModuleResult, RunContext

ModuleFn = Callable[[RunContext], ModuleResult]

_REGISTRY: dict[str, ModuleFn] = {}


def register(name: str) -> Callable[[ModuleFn], ModuleFn]:
    """Decorator: register a module entrypoint under ``name``."""

    def decorator(fn: ModuleFn) -> ModuleFn:
        _REGISTRY[name] = fn
        return fn

    return decorator


def get(name: str) -> ModuleFn | None:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY)


def discover(package: str = "modules") -> None:
    """Import every submodule of ``package`` so their @register calls run."""
    pkg = importlib.import_module(package)
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return
    for info in pkgutil.walk_packages(pkg_path, prefix=f"{package}."):
        importlib.import_module(info.name)
