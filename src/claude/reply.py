"""The base every Claude reply shape subclasses, and the field types the
shapes share.

Each shape lives beside the prompt that asks for it; call_claude_json
(src/claude/api.py) sends the shape's JSON schema and returns an instance.
Imports nothing but pydantic, so src.claude.fit imports it even where its
profile-reading imports fail.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _api_schema(schema, _cls):
    schema.pop("description", None)
    schema["additionalProperties"] = False


class Reply(BaseModel):
    """A reply shape. The schema sent to the API is closed, as structured
    outputs require, and leaves out the class docstring, which is for
    readers, not Claude:

    >>> class Pair(Reply):
    ...     '''For readers only.'''
    ...     name: str
    ...     score: Unit
    >>> sorted(Pair.model_json_schema())
    ['additionalProperties', 'properties', 'required', 'title', 'type']

    A key the schema does not name is dropped, not rejected: it can only
    arrive on the legacy tool-call path, and should not cost an otherwise
    valid answer. Strings arrive stripped, and a Unit score out of range
    clamps, since the schema cannot bound it:

    >>> Pair.model_validate({"name": " Acme ", "score": 1.7, "note": "?"})
    Pair(name='Acme', score=1.0)
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True,
                              json_schema_extra=_api_schema)


#: A 0..1 score (see Reply).
Unit = Annotated[float, AfterValidator(lambda x: min(1.0, max(0.0, x)))]


def choice(*values):
    """A string field the API schema limits to `values`, lower-cased on the
    way in: structured outputs do not guarantee an enum value's case. A
    value outside `values` still validates, for the caller to judge.

    >>> class Seat(Reply):
    ...     seat: choice("ic", "manager")
    >>> Seat.model_json_schema()["properties"]["seat"]["enum"]
    ['ic', 'manager']
    >>> Seat(seat="Manager").seat, Seat(seat="Director").seat
    ('manager', 'director')
    """
    return Annotated[str, AfterValidator(str.lower),
                     Field(json_schema_extra={"enum": list(values)})]
