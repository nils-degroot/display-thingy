"""View system: base class, registry, and auto-discovery."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from display_thingy.config import Settings

log = logging.getLogger(__name__)


class BaseView(ABC):
    """Base class that all display views must implement.

    A view is responsible for:
    1. Fetching whatever data it needs (API calls, file reads, etc.)
    2. Rendering a full 800x480 1-bit PIL Image

    To create a new view:
    1. Create a new .py file in the views/ directory
    2. Subclass BaseView
    3. Call `registry.register(YourView)` at module level
    """

    name: str = ""
    description: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def render(self, width: int, height: int) -> Image.Image:
        """Fetch data and render the display image.

        Args:
            width: Display width in pixels (800).
            height: Display height in pixels (480).

        Returns:
            A PIL Image in mode '1' (1-bit black/white), sized width x height.
        """
        ...


class ViewRegistry:
    """Registry that maps view names to view classes."""

    def __init__(self) -> None:
        self._views: dict[str, type[BaseView]] = {}

    def register(self, view_cls: type[BaseView]) -> type[BaseView]:
        """Register a view class. Can be used as a decorator."""
        if not view_cls.name:
            raise ValueError(f"View class {view_cls.__name__} must define a 'name' attribute")
        self._views[view_cls.name] = view_cls
        log.info("Registered view: %s (%s)", view_cls.name, view_cls.description)
        return view_cls

    def get(self, name: str) -> type[BaseView] | None:
        """Get a view class by name."""
        return self._views.get(name)

    def available(self) -> list[str]:
        """List all registered view names."""
        return list(self._views.keys())


# Global registry instance
registry = ViewRegistry()


def discover_views() -> None:
    """Auto-import all modules in the views package to trigger registration."""
    package = importlib.import_module("display_thingy.views")
    for _importer, module_name, _is_pkg in pkgutil.iter_modules(package.__path__):
        if module_name.startswith("_"):
            continue
        importlib.import_module(f"display_thingy.views.{module_name}")
