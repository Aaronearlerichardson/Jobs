"""The schema of a `config.BOARDS` spec: frozen pydantic models, parsed once
when the engine loads (`parse`). A key's meaning is its `Field` description
and its default is declared on its model, nowhere else.

A model's `ADAPTATIONS` are the keys that adapt the engine to one platform's
behavior; a `why` ("reason, YYYY-MM") may note them. A listing alternative
after the first is a fallback, and needs one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, ClassVar, Literal, Self, Union, cast, get_args, override

from cssselect import SelectorError
from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict,
                      Discriminator, Field, SkipValidation, Strict, StringConstraints, Tag,
                      ValidationError, model_validator)
from typing_extensions import TypedDict

from src import config, validation
from src.match.locality import LocationRE
from src.net.util import JSON, css, xpath
from src.rows import HandleColumn
from . import fields

RowField = Literal["id", "title", "url", "location", "description", "posted_at",
                   "remote_hint", "department"]
ROW_FIELDS = set(get_args(RowField))
#: The row fields a rescue's detail may fill (`Rescue.fields`).
FillField = Literal["location", "description", "posted_at", "remote_hint"]


class EngineRow(TypedDict, total=False, closed=True):
    """A listing entry as a board's row mapper builds it, until `board_jobs`
    turns it into a job. Its names are checked against `ROW_FIELDS` by
    tests/test_board_specs.py::test_the_engine_row_names_the_spec_row_fields.

    Notes:
        `department` is a row field the mapper reads and never stores: it
        goes into `head`, the title the gates read, which `board_jobs` pops.
        `_free` is the rescue's free text, dropped with every `_` key.
    """
    id: str | None
    title: str
    url: str
    location: str
    description: str
    posted_at: str
    remote_hint: str
    head: str
    _free: str


#: A `fields` map: row field (or internal `_field`) -> field spec (fields.py).
Fields = dict[str, JSON]

#: The named request values a template reads ("$area": the pull's location
#: regex, for a decoder that chooses among places).
Vals = TypedDict("Vals", {"$size": int, "$offset": int, "$page": int | None,
                          "$facets": dict[str, list[JSON]], "$search_text": str,
                          "$area": LocationRE | None, "$plain_user_agent": str},
                 total=False, closed=True)


def _css(v: str) -> str:
    """`v` once it compiles as CSS in cssselect's dialect, each {placeholder}
    a sample value; the "$job_links" sentinel passes.

    >>> _css("li:has(.loc) a[href*='/{slug}/']")
    "li:has(.loc) a[href*='/{slug}/']"
    >>> _css("dt:-soup-contains('City') + dd")
    Traceback (most recent call last):
    ValueError: bad CSS: The pseudo-class :-soup-contains() is unknown
    """
    if v != "$job_links":
        try:
            xpath(css(fields.fmt(v, lambda _name: "x")))
        except SelectorError as e:
            raise ValueError(f"bad CSS: {e}") from None
    return v


def _listed(v: JSON) -> JSON:
    """A key taking one value or several: a lone value as a list of one."""
    return [v] if isinstance(v, (str, dict)) else v


Str = Annotated[str, Strict()]
Int = Annotated[int, Strict()]
Bool = Annotated[bool, Strict()]
Count = Annotated[int, Strict(), Field(ge=1)]
Status = Annotated[int, Strict(), Field(ge=100, le=599)]
Regex = Annotated[validation.Regex, Strict()]
Template = Annotated[str, Strict(), StringConstraints(min_length=1),
    AfterValidator(lambda v: [v, fields.check_template(v)][0])]
#: A CSS selector template, compiled as the spec loads (`net.util.css`).
Css = Annotated[Template, AfterValidator(_css)]
#: A field-grammar spec (fields.py): a path, a dict, or None.
Raw = Annotated[JSON, SkipValidation()]
Grammar = Annotated[Raw, AfterValidator(lambda v: [v, fields.check(v)][0])]
Paths = Annotated[tuple[Str, ...], BeforeValidator(_listed)]
#: Search terms each placed among every platform's: [position, text] pairs.
Ranked = tuple[tuple[Int, Str], ...]
Why = Annotated[str, Strict(),
                StringConstraints(pattern=r"^\S.*, 20\d\d-(0[1-9]|1[0-2])( \(inferred\))?$")]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Adaptation(_Spec):
    """A model with `ADAPTATIONS`: keys that adapt the engine to one
    platform's behavior. `why` optionally notes them."""
    ADAPTATIONS: ClassVar[tuple[str, ...]] = ()
    why: Why | None = Field(None, description='Why the adaptation: "reason, YYYY-MM"')


class Canary(_Spec):
    name: Str = Field(description="The employer the sample board belongs to")
    handle: Str = Field(description="The sample board's handle")
    min_jobs: Count = Field(1, description="The fewest postings a healthy board lists")
    min_fill: dict[RowField, Annotated[float, Strict(), Field(ge=0, le=1)]] = Field(
        default_factory=dict, description="Per row field, the share of the board's rows that must fill it, "
                        "over pager.FILL_FLOORS")


class Detect(_Spec):
    re: tuple[Regex, ...] = Field((), description="Regexes that must all match; their groups "
                                                  "are the handle's parts, in order")
    host: Str | None = Field(None, description="The vendor's host; an entry with no `re` "
                                               "names only that")
    transform: tuple[Str, ...] = Field((), description='A fields.TRANSFORMS name per group '
                                                       '("keep": as captured); default all kept')
    blocklist: tuple[Str, ...] = Field((), description="Part values that reject a match")
    careers_url: Template | None = Field(None, description="The board's URL, rebuilt from "
                                                           "the parts or the {page} that "
                                                           "carried the signature")
    off_page: Regex | None = Field(None, description="A page whose URL matches carries no "
                                                     "signature: the vendor's own pages are "
                                                     "not a customer's board")

    @model_validator(mode="after")
    def _groups(self) -> Self:
        groups = sum(re.compile(rx).groups for rx in self.re)
        if not (self.re or self.host):
            raise ValueError("detect: regexes, or a host")
        if self.re and not groups:
            raise ValueError("detect.re captures the handle")
        if self.transform and (len(self.transform) != groups
                               or any(t not in fields.TRANSFORMS for t in self.transform)):
            raise ValueError("detect.transform: a known transform per group")
        return self


class JobRef(_Spec):
    re: Regex = Field(description="Reads a stored posting URL")
    parts: tuple[Str, ...] = Field(("slug", "jid"),
                                   description="Its groups' names: handle parts, then the "
                                               "posting's own (jid, or listing keys its id reads)")

    @model_validator(mode="after")
    def _named(self) -> Self:
        if re.compile(self.re).groups != len(self.parts):
            raise ValueError("job_ref.parts must name every group")
        return self


class _Decoder(_Spec):
    entries: Paths = Field(("",), description="Where the payload keeps its entries: the first "
                                              "path holding a list")
    values: Str | None = Field(None, description="Every dict holding this key stands for its value")

    @property
    def first(self) -> str:
        """A detail answer's record unless `record` says: the first entry."""
        return f"{self.entries[0]}[0]"


class _Json(_Decoder):
    @property
    @override
    def first(self) -> str:
        return ""


class JsonDecoder(_Json):
    kind: Literal["json"] = Field("json", description="The payload itself")


class JsonInHtmlDecoder(_Json):
    kind: Literal["json_in_html"] = Field(description="One JSON value on a page")
    regex: Regex | None = Field(None, description="Ends just before the JSON in the page text")
    element: Css | None = Field(None, description="Else: the CSS of the element whose "
                                                  "`attribute` (entity-decoded) is the JSON")
    attribute: Str = Field("value", description="The `element` attribute holding the JSON")

    @model_validator(mode="after")
    def _one_locator(self) -> Self:
        if (self.regex is None) == (self.element is None):
            raise ValueError("json_in_html: name exactly one of regex, element")
        return self


class JsonLdDecoder(_Decoder):
    kind: Literal["jsonld"] = Field(description="The page's schema.org JobPostings")
    entries: Paths = Field(("postings",), description="Where the decoded postings sit")
    cells: dict[Str, Css] = Field(default_factory=dict,
                           description='{name: CSS}: a page naming no posting, as '
                                       '"page", the text of each')


class AtomDecoder(_Decoder):
    kind: Literal["atom"] = Field(description="An Atom feed's entries")
    entries: Paths = Field(("entries",), description="Where the decoded entries sit")


class XmlDecoder(_Decoder):
    kind: Literal["xml"] = Field(description="One record per XML element of a local name "
                                             "(decode.xml_records); CDATA is text")
    entries: Paths = Field(("records",), description="Where the decoded records sit")
    select: Str = Field(description="The local name of the record elements")
    lists: tuple[Str, ...] = Field((), description="Child names read as a list of every "
                                                   "occurrence, however many there are")


class HtmlDecoder(_Decoder):
    kind: Literal["html"] = Field(description="One entry per selected element (decode.elements)")
    entries: Paths = Field(("elements",), description="Where the decoded elements sit")
    select: Annotated[tuple[Css, ...], BeforeValidator(_listed)] = Field(
        min_length=1, description="CSS templates (cssselect's dialect: :contains, not "
                                  ':-soup-contains) tried in order until one finds any; '
                                  '"$job_links": the careers-page reader (custom.read_page)')
    context: Literal["parent", "lines"] | tuple[Str, ...] | None = Field(
        None, description="The block around an element: its parent, the nearest of these "
                          "tags, or the nearest holding two lines")
    cells: dict[Str, Css] = Field(default_factory=dict,
                           description="{name: CSS}: text found in the context")
    base: Template | None = Field(None, description="Makes hrefs absolute; default the page")
    selects: Bool = Field(False, description='Also the page\'s <select> fields, as "selects": '
                                             '[{name, options: [{value, label}]}]')


def _decoder_kind(v: object) -> str | None:
    """A decoder's `kind`: a dict naming none is JsonDecoder's default."""
    if isinstance(v, dict):
        return cast(str | None, v["kind"] if "kind" in v else JsonDecoder.model_fields["kind"].default)
    return getattr(v, "kind", None)


Decoder = Annotated[Union[Annotated[JsonDecoder, Tag("json")],
                          Annotated[JsonInHtmlDecoder, Tag("json_in_html")],
                          Annotated[JsonLdDecoder, Tag("jsonld")],
                          Annotated[AtomDecoder, Tag("atom")],
                          Annotated[XmlDecoder, Tag("xml")],
                          Annotated[HtmlDecoder, Tag("html")]],
                    Discriminator(_decoder_kind)]


class _Pager(_Adaptation):
    ADAPTATIONS: ClassVar[tuple[str, ...]] = ("ceiling",)
    pages: Count = Field(10, description="The most pages a walk reads")
    total: Grammar = Field(None, description="Names the board's total on the first page")
    ceiling: Count | None = Field(None, description="The most rows the server serves: a total "
                                                    "there ends nothing")
    declared: Grammar = Field(None, description="Names the last page, on every page")

    @property
    def stride(self) -> int | None:
        """Rows between one page's first row and the next's."""
        raise NotImplementedError

    def number(self, n: int) -> int:
        """Page `n`'s (from 0) own number."""
        return n

    def page(self, n: int) -> int | None:
        """The "$page" value page `n` asks for."""
        return self.number(n)

    def offset(self, n: int, size: int) -> int:
        """The "$offset" value page `n` of `size` rows asks for."""
        return n * size


class _SizedPager(_Pager):
    """A pager that must say how many rows it asks per page."""
    size: Count = Field(description="Rows asked per page")

    @property
    @override
    def stride(self) -> int | None:
        return self.size


class OffsetPager(_Pager):
    kind: Literal["offset"] = Field(description='"$offset" steps a page')
    size: Count | None = Field(
        None, description="Rows asked per page; unset, the server sizes its pages and the walk "
                          "learns it (pager.walk)")
    start: Annotated[Int, Field(ge=0)] = Field(0, description="The first row's offset")

    @property
    @override
    def stride(self) -> int | None:
        return self.size

    @override
    def offset(self, n: int, size: int) -> int:
        return self.start + n * size


class OverlapPager(_SizedPager):
    ADAPTATIONS = ("ceiling", "step")
    kind: Literal["overlap"] = Field(description='"$offset" steps `step` < `size`: pages overlap')
    step: Count = Field(description="Rows each page steps")

    @model_validator(mode="after")
    def _overlaps(self) -> Self:
        if self.step >= self.size:
            raise ValueError("pager: an overlap steps less than a page")
        return self

    @property
    @override
    def stride(self) -> int:
        return self.step

    @override
    def offset(self, n: int, size: int) -> int:
        """Steps `step` in `size` rows: a server serving pages smaller than
        asked keeps the same overlap."""
        return n * max(1, size * self.step // self.size)


class PagePager(_Pager):
    ADAPTATIONS = ("ceiling", "bare_first")
    kind: Literal["page"] = Field(description='"$page" counts pages')
    size: Count | None = Field(None, description="Rows a page holds, when known")
    start: Annotated[Int, Field(ge=0)] = Field(0, description="The first page's number")
    bare_first: Bool = Field(False, description="The first page's request names no page")

    @property
    @override
    def stride(self) -> int | None:
        return self.size

    @override
    def number(self, n: int) -> int:
        return self.start + n

    @override
    def page(self, n: int) -> int | None:
        return None if n == 0 and self.bare_first else self.start + n


class CursorPager(_SizedPager):
    kind: Literal["cursor"] = Field(description="Each page names the next, followed verbatim")
    next: Str = Field(description="The path of a page's next-page URL")
    has_next: Str = Field(description="The path of a page's more-to-come flag")


Pager = Annotated[Union[OffsetPager, OverlapPager, PagePager, CursorPager],
                  Field(discriminator="kind")]


class Scope(_Spec):
    kind: Literal["facets"] = Field(description="Narrow by the facet values the area matches")
    facets: Str = Field(description="The facet groups' path on an unscoped first page")
    param: Str = Field(description="A group's (or value's) parameter name key")
    param_re: Regex = Field(description="The groups whose parameter it matches")
    values: Str = Field(description="A group's (or value's nested) values key")
    id: Str = Field(description="A value's id key")
    label: Str = Field(description="A value's label key, matched against the area")


class _Call(_Spec):
    """One request. The config key `json` fills `json_`:

    >>> _Call.model_validate({"url": "https://x.test/", "json": {"q": "{search_text}"}}).json_
    {'q': '{search_text}'}
    """
    url: Template = Field(description="The request's URL template")
    method: Literal["GET", "POST"] = Field("GET", description="The HTTP method")
    params: dict[Str, Str] | None = Field(None, description="Query parameters; a None one is "
                                                            "left off")
    json_: dict[Str, Raw] | None = Field(None, alias="json", description="A JSON body template")
    headers: dict[Str, Str] = Field(default_factory=dict,
                                description="Over the shared request headers")
    decoder: Decoder = Field(JsonDecoder(),
                             description="Reads the response body")


class _Request(_Call):
    fields: dict[Str, Grammar] = Field(default_factory=dict,
                                description="Row field (or internal _field) -> field spec")

    @model_validator(mode="after")
    def _row_fields(self) -> Self:
        extra = sorted(k for k in self.fields if k not in ROW_FIELDS and not k.startswith("_"))
        if extra:
            raise ValueError(f"unknown field(s) {extra}")
        return self


class Prelude(_Call):
    set: dict[Str, Str] = Field(min_length=1, description="{part: path in the decoded answer}: "
                                                          "the parts one answer settles")


class Accept(_Spec):
    status: tuple[Status, ...] | None = Field(None, description="Only these statuses; default any")
    status_not: tuple[Status, ...] = Field((), description="None of these statuses")
    total: Bool = Field(False, description="A listing answer carries an int total")


class Handle(_Adaptation):
    """A board's handle: its columns, and the adaptations that settle it.

    The config key `try` fills `try_`:

    >>> Handle.model_validate({"try": {"host": ["a", "b"]}}).try_
    {'host': ('a', 'b')}
    """
    ADAPTATIONS = ("try_", "accept", "prelude")
    columns: tuple[HandleColumn, ...] = Field(
        config.DEFAULT_HANDLE_COLUMNS, min_length=1,
        description="The store columns naming the board; `handle` alone holds a handle of "
                    "several parts, `sep`-joined (a hit carries it as a tuple)")
    parts: tuple[Str, ...] = Field((), description="The handle's pieces' names; default the "
                                                   "columns")
    sep: Str = Field("|", description="Joins the parts into one handle string")
    fold: Bool = Field(False, description="The host answers a handle's case alike, so boards "
                                          "differing only in case are one board")
    try_: dict[Str, Annotated[tuple[Template, ...], Field(min_length=1)]] = Field(
        default_factory=dict, alias="try", max_length=1,
        description="One part's templates, tried until an answer `accept` allows; "
                    "settled once per handle")
    accept: Accept = Field(Accept(),
                           description="The answers that settle a `try` value")
    follow: dict[Str, Annotated[tuple[Template, ...], BeforeValidator(_listed),
                                Field(min_length=1)]] = Field(
        default_factory=dict,
        description="A part that is the redirect target of its URL template, or of the "
                    "first of several that answers 200 (a root that refuses a client, "
                    "then a known path); settled once per handle")
    prelude: tuple[Prelude, ...] = Field(
        (), description="Requests answering parts a listing or detail request needs (a token "
                        "its own page or API hands out), each part settled once per handle "
                        "and again, once, when a request is refused 401 or 403")

    @model_validator(mode="after")
    def _prelude_parts(self) -> Self:
        """A prelude settles parts nothing else names."""
        taken = [*self.names, *self.follow, *self.try_, *(n for pre in self.prelude for n in pre.set)]
        if len(taken) != len(set(taken)):
            raise ValueError("handle.prelude: a part is settled by one source")
        return self

    @model_validator(mode="after")
    def _handle_alone(self) -> Self:
        """The `handle` column holds the whole handle."""
        if "handle" in self.columns and self.columns != ("handle",):
            raise ValueError("handle.columns: the `handle` column stands alone")
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return self.parts or self.columns


class Listing(_Request):
    probe_url: Template | None = Field(None, description="The URL a cheap read of ids, titles "
                                                         "and the total asks instead")
    pager: Pager | None = Field(None, description="How pages step; none reads one page")
    scope: Scope | None = Field(None, description="Narrows the listing to the locality")
    why: Why | None = Field(None, description='Why this fallback alternative exists: '
                                              '"reason, YYYY-MM"')
    missing_at: Regex | None = Field(None, description="A final URL this matches means the "
                                     "handle names no board: the vendor redirects an unknown "
                                     "one to its own site. Reported as HTTP 404")


class Detail(_Request):
    record: Paths | None = Field(None, description="The record's paths in an answer; default "
                                                   "the decoder's first entry")
    location: Literal["always", "if_unknown", "never"] = Field(
        "never", description="When the detail's location replaces the row's")


class Rescue(_Spec):
    when: Literal["scoped", "always"] = Field("scoped", description="On a scoped pull, or "
                                                                    "every pull")
    unknown: Regex = Field(description="A listed location it matches is filled from the detail")
    cap: Count = Field(description="The most detail reads a pull spends")
    cache_days: Annotated[Int, Field(ge=0)] = Field(0, description="Days a found location is "
                                                                   "cached; 0 none")
    free: Grammar = Field(None, description="Free text tried against the area first")
    fields: tuple[FillField, ...] = Field(("location",), description="The row fields the "
                                                                     "detail fills")
    why: Why = Field(description='Why the listing needs rescuing: "reason, YYYY-MM"')


class Rule(_Spec):
    when: Annotated[dict[Str, Raw],
    AfterValidator(lambda v: [v, fields.check({"const": 1, "when": v})][0])] = Field(
        description="A condition on the record")
    why: Grammar = Field(None, description="Names the reason")


#: A closure condition is its one rule; a rule list is as it is.
Rules = Annotated[tuple[Rule, ...],
                  BeforeValidator(lambda v: [{"when": v}] if isinstance(v, dict) else v)]


class Closure(_Adaptation):
    ADAPTATIONS = ("url", "unmatched")
    via: Literal["detail", "listing", "page"] | None = Field(
        None, description="What judges a posting; default the detail where there is one, "
                          "else its page")
    url: Template | None = Field(None, description="Asked in place of the detail's URL")
    open: Rules = Field((), description="A condition, or rules, proving the posting live")
    closed: Rules = Field((), description="A condition, or rules, proving it closed")
    unmatched: Str | None = Field(None, description="The reason a readable answer neither "
                                                    "rule matches closes the posting")
    page_closed: Regex | None = Field(None, description="A match on the posting's own page "
                                                        "proving it closed")


def _completed(first: dict[str, object], alt: dict[str, object]) -> dict[str, object]:
    """Listing alternative `alt` with what it neither sets nor names in
    its `reset` taken from `first`.

    >>> _completed({"url": "a", "pager": {}}, {"url": "b", "reset": ["pager"]})
    {'url': 'b'}
    >>> _completed({"url": "a"}, {"reset": ["pager"]})
    Traceback (most recent call last):
    ValueError: listing reset ['pager']: keys the first alternative sets and this one does not
    """
    reset = alt.get("reset", [])
    if not (isinstance(reset, list)
            and all(isinstance(k, str) and k in first and k not in alt for k in reset)):
        raise ValueError(f"listing reset {reset!r}: keys the first alternative sets and this "
                         "one does not")
    return {**{k: v for k, v in first.items() if k not in alt and k not in reset},
            **{k: v for k, v in alt.items() if k != "reset"}}


class Discovery(_Spec):
    scan: Bool = Field(False, description="No name guess reaches the handle: discovery reads it "
                                          "off the company's careers pages, fetched and then "
                                          "rendered in a headless browser")
    shared: Bool = Field(False, description="A board can be a parent company's, serving its "
                                            "subsidiaries: one sharing no token with the "
                                            "company's name is asked who owns it")
    narrow: Bool = Field(False, description="The vendor's host serves so many employers that a "
                                            "bare site: search of it is noise: the dork "
                                            "searches its `search` forms by name, with the "
                                            "profile's domain keywords")
    search: Ranked = Field((), description="The host forms the dork searches with site:, "
                                           "every platform's in position order")
    hint: Ranked = Field((), description="The vendor terms the web-search resolver ORs into "
                                         "its board query, every platform's in position order")


class BoardSpec(_Spec):
    sweep: Bool = Field(False, description="The lightweight sweep pulls it whole")
    prunable: Bool = Field(False, description="prune_dead_boards may deactivate it")
    guess: Bool = Field(False, description="Discovery may guess its handle from a name")
    eager: Bool = Field(False, description="A whole-board pull reads each kept row's detail")
    handle: Handle = Field(Handle(),
                           description="How a store row names the board")
    job_ref: JobRef | None = Field(None, description="Reads a stored posting URL")
    listing: tuple[Listing, ...] = Field(
        (), description="Alternatives tried in order until one yields a posting, each later "
                        "one taking what it does not set from the first, but for the keys "
                        "its `reset` names")
    rescue: Rescue | None = Field(None, description="Fills vague listed rows from the detail")
    detail: Detail | None = Field(None, description="Reads one posting back")
    closure: Closure = Field(Closure(),
                             description="Judges a stored posting open or closed")
    employer: Grammar = Field(None, description="Names the employer on a listing entry")
    unlocated: Literal["drop", "keep"] = Field(
        "drop", description="A location filter's verdict on a row naming no place")
    detect: tuple[Detect, ...] = Field((), description="How a URL or page names the board")
    canary: Canary | None = Field(None, description="The public board tools/check_boards.py "
                                                    "probes")
    discovery: Discovery = Field(Discovery(),
                                 description="How discovery finds and vets a board")

    @model_validator(mode="before")
    @classmethod
    def _alternatives(cls, data: object) -> object:
        """A lone listing as the one alternative; a later one completed
        from the first (`_completed`)."""
        if not isinstance(data, dict):
            return data
        listing = data.get("listing")
        if isinstance(listing, dict):
            return {**data, "listing": [listing]}
        if not (isinstance(listing, list) and listing
                and all(isinstance(alt, dict) for alt in listing)):
            return data
        first, *rest = listing
        if "reset" in first:
            raise ValueError("listing[0].reset: the first alternative inherits nothing")
        return {**data, "listing": [first, *(_completed(first, alt) for alt in rest)]}

    @model_validator(mode="after")
    def _fallbacks_explained(self) -> Self:
        for i, alt in enumerate(self.listing):
            if i == 0 and alt.why is not None:
                raise ValueError("listing[0].why: the first alternative is no fallback")
            if i and alt.why is None:
                raise ValueError(f'listing[{i}]: a fallback; say why ("reason, YYYY-MM")')
        return self

    @model_validator(mode="after")
    def _closure_servable(self) -> Self:
        if self.via == "detail" and not self.detail:
            raise ValueError("closure.via detail needs a detail")
        if self.via == "listing" and (not self.listing or any(a.pager for a in self.listing)):
            raise ValueError("closure.via listing reads one page: its listing cannot page")
        return self

    @model_validator(mode="after")
    def _rescue_servable(self) -> Self:
        scoped = self.listing and self.listing[0].scope
        if self.rescue and not (self.detail and (scoped or self.rescue.when == "always")):
            raise ValueError("rescue: a detail, and a scoped listing unless `when` is always")
        return self

    @property
    def via(self) -> Literal["detail", "listing", "page"]:
        """What judges a stored posting (`closure.via`, resolved)."""
        return self.closure.via or self.default_via

    @property
    def default_via(self) -> Literal["detail", "page"]:
        """`closure.via` unless set: the detail where there is one, else the page."""
        return "detail" if self.detail else "page"


def parse(name: str, raw: JSON) -> BoardSpec:
    """`raw`, a `config.BOARDS` entry, as a BoardSpec; ValueError naming
    `name` and every broken key's path when it breaks the schema."""
    try:
        return BoardSpec.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{name}: {e}") from None


def walk(model: BaseModel, path: str = "") -> Iterator[tuple[str, object, bool, object]]:
    """(path, value, set, default) for every key of `model` and of the
    models under it, depth first; `set` when the spec gave the key."""
    for name, info in type(model).model_fields.items():
        key, value = f"{path}{info.alias or name}", getattr(model, name)
        default = info.get_default(call_default_factory=True)
        yield key, value, name in model.model_fields_set, default
        if isinstance(value, BaseModel):
            yield from walk(value, f"{key}.")
        elif isinstance(value, tuple):
            for i, sub in enumerate(value):
                if isinstance(sub, BaseModel):
                    yield from walk(sub, f"{key}[{i}].")
