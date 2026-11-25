"""Fake file tools. Real handlers would touch disk; the parity driver replaces them with stubs."""
from tools.registry import registry

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "offset": {"type": "integer", "description": "First line", "default": 1},
            "limit": {"type": "integer", "description": "Max lines", "default": 2000},
        },
        "required": ["path"],
    },
}

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents with a regex.",
    "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string", "description": "Regex"}},
        "required": ["pattern"],
    },
}


def _refuse(*_a, **_k):
    raise RuntimeError("fake tree: real tool handler must never run under hermes-parity")


registry.register("read_file", "file", READ_FILE_SCHEMA, _refuse)
registry.register("search_files", "file", SEARCH_FILES_SCHEMA, _refuse)
