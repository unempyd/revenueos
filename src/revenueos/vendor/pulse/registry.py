"""Function-calling tools — @tool decorator + ToolRegistry.

Ported verbatim from perso/iris. Parses Google-style docstrings into JSON schemas
the OpenAI chat-completions API understands.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints


def _split_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    if not doc:
        return "", {}
    text = inspect.cleandoc(doc)
    lines = text.splitlines()
    description_lines: list[str] = []
    params: dict[str, str] = {}
    in_args = False
    for line in lines:
        if re.match(r"^(Args|Arguments|Parameters):\s*$", line):
            in_args = True
            continue
        if in_args:
            m = re.match(r"^\s+(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", line)
            if m:
                params[m.group(1)] = m.group(2).strip()
                continue
            if line.strip() == "":
                continue
            in_args = False
            description_lines.append(line)
        else:
            description_lines.append(line)
    description = "\n".join(description_lines).strip()
    return description, params


def _type_to_schema(t: Any) -> dict[str, Any]:
    origin = get_origin(t)
    if t is str:
        return {"type": "string"}
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if t is bool:
        return {"type": "boolean"}
    if origin is Literal:
        values = list(get_args(t))
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        return {"enum": values}
    if origin in (list,):
        args = get_args(t)
        item_schema = _type_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    if origin in (dict,):
        return {"type": "object"}
    if origin is Union:
        non_none = [a for a in get_args(t) if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])
    return {"type": "string"}


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: list[str]
    fn: Callable[..., Any]

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }

    async def call(self, arguments: dict[str, Any]) -> str:
        result = self.fn(**arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str, ensure_ascii=False)


def tool(fn: Callable[..., Any]) -> Tool:
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    description, param_docs = _split_docstring(fn.__doc__)
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        py_type = hints.get(pname, str)
        schema = _type_to_schema(py_type)
        if pname in param_docs:
            schema["description"] = param_docs[pname]
        properties[pname] = schema
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return Tool(
        name=fn.__name__,
        description=description or fn.__name__,
        properties=properties,
        required=required,
        fn=fn,
    )


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, t: Tool) -> None:
        if t.name in self._tools:
            raise ValueError(f"duplicate tool name: {t.name}")
        self._tools[t.name] = t

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [t.openai_definition() for t in self._tools.values()]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        t = self._tools.get(name)
        if t is None:
            return f"error: unknown tool '{name}'"
        try:
            return await t.call(arguments)
        except Exception as e:
            return f"error calling {name}: {type(e).__name__}: {e}"
