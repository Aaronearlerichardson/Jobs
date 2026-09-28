"""The base every Claude reply shape subclasses, and the score type the
shapes share.

Each shape lives beside the prompt that asks for it; call_claude_json
(src/claude/api.py) sends the shape's JSON schema and returns an instance.
Imports nothing but pydantic, so src.claude.fit imports it even where its
profile-reading imports fail.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict


def _api_schema(schema: dict[str, Any], _cls: type[BaseModel]) -> None:
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

    An enum field is `OneOf(values, loose=True)`: the schema names
    `values`, and a reply outside them is the caller's to judge:

    >>> from src.validation import OneOf
    >>> class Seat(Reply):
    ...     seat: Annotated[str, OneOf(("ic", "manager"), loose=True)]
    >>> Seat(seat="Director").seat, Seat.model_json_schema()["properties"]["seat"]["enum"]
    ('director', ['ic', 'manager'])
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True,
                              json_schema_extra=_api_schema)


#: A 0..1 score (see Reply).
Unit = Annotated[float, AfterValidator(lambda x: min(1.0, max(0.0, x)))]
