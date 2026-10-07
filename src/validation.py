"""The pydantic field types and helpers more than one schema uses. The
lowest layer: it imports nothing from src, so any module may use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import (AfterValidator, BeforeValidator, GetCoreSchemaHandler,
                      GetJsonSchemaHandler, ValidationError)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, ErrorDetails, core_schema


def error_lines(err: ValidationError) -> list[str]:
    """`err` as one 'path: problem' line per error, never quoting the bad
    value.

    >>> from pydantic import TypeAdapter
    >>> try:
    ...     TypeAdapter(dict[str, Regex]).validate_python({"a": "(", "b": "ok"})
    ... except ValidationError as e:
    ...     error_lines(e)
    ['a: not a valid regex (missing ), unterminated subpattern at position 0)']
    """
    return [_line(x) for x in err.errors(include_url=False, include_input=False)]


def _line(err: ErrorDetails) -> str:
    path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                   for p in err["loc"]).lstrip(".")
    ctx = err.get("ctx") or {}
    msg = {"missing": "required", "extra_forbidden": "unknown key",
           "model_type": "must be a table", "dict_type": "must be a table",
           }.get(err["type"]) or str(ctx.get("error") or err["msg"])
    return f"{path}: {msg}" if path else msg


def _regex(v: str) -> str:
    try:
        re.compile(v)
    except re.error as e:
        raise ValueError(f"not a valid regex ({e})") from None
    return v


def blank_is_none(v: object) -> object:
    """`v` stripped when a str, and None when that leaves nothing.

    >>> blank_is_none(" x "), blank_is_none(" \\t"), blank_is_none(0)
    ('x', None, 0)
    """
    return (v.strip() or None) if isinstance(v, str) else v


def drop_blank(data: object) -> object:
    """A model's raw input less the values `blank_is_none` makes None, the
    rest stripped: for a `mode="before"` validator where blank means unset.

    >>> drop_blank({"a": " x ", "b": " ", "c": None, "d": 0}), drop_blank("raw")
    ({'a': 'x', 'd': 0}, 'raw')
    """
    if not isinstance(data, dict):
        return data
    return {k: b for k, v in data.items() if (b := blank_is_none(v)) is not None}


Regex = Annotated[str, AfterValidator(_regex)]
Text = Annotated[str | None, BeforeValidator(blank_is_none)]


@dataclass(frozen=True)
class OneOf:
    """`Annotated[str, OneOf(values)]`: a str lower-cased on the way in,
    whose JSON schema enumerates `values`, refused outside them unless
    `loose`.

    >>> from pydantic import TypeAdapter
    >>> seat = TypeAdapter(Annotated[str, OneOf(("ic", "manager"))])
    >>> seat.validate_python("Manager"), seat.json_schema()
    ('manager', {'enum': ['ic', 'manager'], 'type': 'string'})
    >>> try:
    ...     seat.validate_python("director")
    ... except ValidationError as e:
    ...     error_lines(e)
    ['expected one of ic, manager']
    >>> TypeAdapter(Annotated[str, OneOf(("ic",), loose=True)]).validate_python("Director")
    'director'

    Notes:
        Type checkers see a plain str, where a Literal of a runtime tuple
        (the pattern this replaces) needed a `type: ignore` at every use.
    """
    values: tuple[str, ...]
    loose: bool = False

    def __get_pydantic_core_schema__(self, source: object,
                                     handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.no_info_after_validator_function(self._check, handler(source))

    def __get_pydantic_json_schema__(self, schema: CoreSchema,
                                     handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        return {**handler(schema), "enum": list(self.values)}

    def _check(self, v: str) -> str:
        v = v.lower()
        if self.loose or v in self.values:
            return v
        raise ValueError(f"expected one of {', '.join(self.values)}")
