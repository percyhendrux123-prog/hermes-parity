"""Fake tool registry with the same seam the driver stubs (``registry._tools`` of dataclasses)."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Callable


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, name, toolset, schema, handler):
        self._tools[name] = ToolEntry(name, toolset, schema, handler)


registry = Registry()
