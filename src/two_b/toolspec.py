"""Canonical tool schema — defined once, serialized to each provider's format.

The 5 tools themselves live (frozen) in tools.py. Here they're expressed as
provider-agnostic ToolSpec objects that every adapter serializes from, so the
schema never drifts between providers. to_openai() is asserted at import time to
reproduce tools.TOOLS byte-for-byte, guaranteeing the proven local-Ollama
payload is unchanged.
"""
from dataclasses import dataclass
from typing import Any

from . import tools


@dataclass(frozen=True, slots=True)
class ToolParam:
    name: str
    type: str
    description: str = ""      # "" -> omit the description key (matches tools.TOOLS)
    required: bool = True


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    params: tuple[ToolParam, ...] = ()
    # MCP tools carry an arbitrary JSON Schema that ToolParam can't represent
    # (nesting, enums, arrays). When set, it's used verbatim as the parameter
    # schema; the built-in 5 leave it None so their serialized output is unchanged.
    raw_schema: dict | None = None

    def _properties(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for p in self.params:
            entry: dict[str, Any] = {"type": p.type}
            if p.description:
                entry["description"] = p.description
            props[p.name] = entry
        return props

    def _required(self) -> list[str]:
        return [p.name for p in self.params if p.required]

    def _schema(self) -> dict[str, Any]:
        if self.raw_schema is not None:
            return self.raw_schema
        return {"type": "object", "properties": self._properties(), "required": self._required()}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("list_files",
             "List files under a directory, recursively, relative to the current working directory.",
             (ToolParam("path", "string", "Directory to list, e.g. 'lib/agent'"),)),
    ToolSpec("read_file",
             "Read the full text contents of a file.",
             (ToolParam("path", "string"),)),
    ToolSpec("search_files",
             "Search file contents for a literal substring across the project, recursively. "
             "Use this to find where something is defined or used before reading files one by one.",
             (ToolParam("query", "string", "Literal text to search for, e.g. 'MemoryScopeLevel'"),
              ToolParam("path", "string", "Directory to search under (default: '.')", required=False))),
    ToolSpec("edit_file",
             "Replace an exact snippet of text in a file with new text, without rewriting the whole "
             "file. Prefer this over write_file for existing files, especially large ones — it's "
             "faster and lower-risk. old_text must match exactly once in the file.",
             (ToolParam("path", "string"),
              ToolParam("old_text", "string", "The exact existing text to replace."),
              ToolParam("new_text", "string", "The text to replace it with."))),
    ToolSpec("write_file",
             "Overwrite a file with new full contents. Only for new files, or existing files small "
             "enough to safely reproduce in full. Prefer edit_file for existing files, especially "
             "large ones — regenerating a whole large file is slow and risks a truncated/incorrect result.",
             (ToolParam("path", "string"),
              ToolParam("content", "string", "The complete new file contents."))),
)


def to_openai(specs: tuple[ToolSpec, ...] = TOOL_SPECS) -> list[dict]:
    """OpenAI / Ollama / OpenRouter / Mistral / NVIDIA function-tool shape."""
    return [
        {"type": "function", "function": {
            "name": s.name, "description": s.description, "parameters": s._schema(),
        }}
        for s in specs
    ]


def to_anthropic(specs: tuple[ToolSpec, ...] = TOOL_SPECS) -> list[dict]:
    return [
        {"name": s.name, "description": s.description, "input_schema": s._schema()}
        for s in specs
    ]


def to_gemini(specs: tuple[ToolSpec, ...] = TOOL_SPECS) -> list[dict]:
    """Gemini wants one tools entry holding functionDeclarations."""
    return [{"functionDeclarations": [
        {"name": s.name, "description": s.description, "parameters": s._schema()}
        for s in specs
    ]}]


# Guarantee the local-Ollama payload is byte-identical to the proven constant.
assert to_openai() == tools.TOOLS, "toolspec.to_openai() drifted from tools.TOOLS"
