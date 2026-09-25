"""The schema of a `config.BOARDS` spec: frozen pydantic models, parsed once
when the engine loads (`parse`). A key's meaning is its `Field` description
and its default is declared on its model, nowhere else.

A key that exists only because one platform misbehaves (a model's
`WORKAROUNDS`, or a listing alternative after the first) needs a `why`,
"reason, YYYY-MM", beside it.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal, Union, get_args

from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict,
                      Discriminator, Field, Strict, StringConstraints, Tag,
                      ValidationError, model_validator)

from src import config
from . import fields

RowField = Literal["id", "title", "url", "location", "description", "posted_at",
                   "remote_hint", "department"]
ROW_FIELDS = set(get_args(RowField))


def _grammar(v):
    fields.check(v)
    return v


def _template(v):
    fields.check_template(v)
    return v


def _regex(v):
    try:
        re.compile(v)
    except re.error as e:
        raise ValueError(f"bad regex: {e}") from None
    return v


def _condition(v):
    fields.check({"const": 1, "when": v})
    return v


def _listed(v):
    """A key taking one value or several: a lone value as a list of one."""
    return [v] if isinstance(v, (str, dict)) else v


def _ruled(v):
    """A closure condition as its one rule; a rule list as it is."""
    return [{"when": v}] if isinstance(v, dict) else v


Str = Annotated[str, Strict()]
Int = Annotated[int, Strict()]
Bool = Annotated[bool, Strict()]
Count = Annotated[int, Strict(), Field(ge=1)]
Status = Annotated[int, Strict(), Field(ge=100, le=599)]
Regex = Annotated[str, Strict(), AfterValidator(_regex)]
Template = Annotated[str, Strict(), StringConstraints(min_length=1), AfterValidator(_template)]
#: A field-grammar spec (fields.py): a path, a dict, or None.
Grammar = Annotated[Any, AfterValidator(_grammar)]
Paths = Annotated[tuple[Str, ...], BeforeValidator(_listed)]
#: Search terms each placed among every platform's: [position, text] pairs.
Ranked = tuple[tuple[Int, Str], ...]
Why = Annotated[str, Strict(),
                StringConstraints(pattern=r"^\S.*, 20\d\d-(0[1-9]|1[0-2])( \(inferred\))?$")]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Workaround(_Spec):
    """A model whose `WORKAROUNDS` keys, when set, need its `why`; a `why`
    with none set is refused too."""
    WORKAROUNDS: ClassVar[tuple[str, ...]] = ()
    why: Why | None = Field(None, description='Why a workaround key is set: "reason, YYYY-MM"')

    @model_validator(mode="after")
    def _explained(self):
        used = [type(self).model_fields[k].alias or k for k in self.WORKAROUNDS
                if k in self.model_fields_set]
        if used and self.why is None:
            raise ValueError(f'{", ".join(used)}: a workaround; say why ("reason, YYYY-MM")')
        if self.why is not None and not used:
            raise ValueError("why: explains a workaround key, and none is set")
        return self


class Canary(_Spec):
    name: Str = Field(description="The employer the sample board belongs to")
    handle: Str = Field(description="The sample board's handle")
    min_jobs: Count = Field(1, description="The fewest postings a healthy board lists")


class Detect(_Spec):
    re: tuple[Regex, ...] = Field((), description="Regexes that must all match; their groups "
                                                  "are the handle's parts, in order")
    host: Str | None = Field(None, description="The vendor's host; an entry with no `re` "
                                               "names only that")
    transform: tuple[Str | None, ...] = Field((), description="A fields.TRANSFORMS name (or "
                                                              "None) per group; default none")
    blocklist: tuple[Str, ...] = Field((), description="Part values that reject a match")
    careers_url: Template | None = Field(None, description="The board's URL, rebuilt from "
                                                           "the parts or the {page} that "
                                                           "carried the signature")

    @model_validator(mode="after")
    def _groups(self):
        groups = sum(re.compile(rx).groups for rx in self.re)
        if not (self.re or self.host):
            raise ValueError("detect: regexes, or a host")
        if self.re and not groups:
            raise ValueError("detect.re captures the handle")
        if self.transform and (len(self.transform) != groups or any(
                t is not None and t not in fields.TRANSFORMS for t in self.transform)):
            raise ValueError("detect.transform: a known transform or None per group")
        return self


class JobRef(_Spec):
    re: Regex = Field(description="Reads a stored posting URL")
    parts: tuple[Str, ...] = Field(("slug", "jid"),
                                   description="Its groups' names: handle parts, then the "
                                               "posting's own (jid, or listing keys its id reads)")

    @model_validator(mode="after")
    def _named(self):
        if re.compile(self.re).groups != len(self.parts):
            raise ValueError("job_ref.parts must name every group")
        return self


class Accept(_Spec):
    status: tuple[Status, ...] | None = Field(None, description="Only these statuses; default any")
    status_not: tuple[Status, ...] = Field((), description="None of these statuses")
    total: Bool = Field(False, description="A listing answer carries an int total")


class Handle(_Workaround):
    WORKAROUNDS = ("try_", "accept")
    columns: tuple[Str, ...] = Field(config.DEFAULT_HANDLE_COLUMNS, min_length=1,
                                     description="The store columns naming the board")
    parts: tuple[Str, ...] = Field((), description="The handle's pieces' names; default the "
                                                   "columns")
    sep: Str = Field("|", description="Joins the columns into one handle string")
    try_: dict[Str, tuple[Template, ...]] = Field(
        {}, alias="try", max_length=1,
        description="One part's templates, tried until an answer `accept` allows; "
                    "settled once per handle")
    accept: Accept = Field(Accept(), description="The answers that settle a `try` value")
    follow: dict[Str, Template] = Field({}, description="A part that is the redirect target of "
                                                       "its URL template; settled once per handle")

    @property
    def names(self):
        return self.parts or self.columns


class _Decoder(_Spec):
    entries: Paths = Field(("",), description="Where the payload keeps its entries: the first "
                                              "path holding a list")
    values: Str | None = Field(None, description="Every dict holding this key stands for its value")

    @property
    def first(self):
        """A detail answer's record unless `record` says: the first entry."""
        return f"{self.entries[0]}[0]"


class _Json(_Decoder):
    @property
    def first(self):
        return ""


class JsonDecoder(_Json):
    kind: Literal["json"] = Field("json", description="The payload itself")


class JsonInHtmlDecoder(_Json):
    kind: Literal["json_in_html"] = Field(description="One JSON value on a page")
    regex: Regex = Field(description="Ends just before the JSON")


class JsonLdDecoder(_Decoder):
    kind: Literal["jsonld"] = Field(description="The page's schema.org JobPostings")
    entries: Paths = Field(("postings",), description="Where the decoded postings sit")
    cells: dict[Str, Str] = Field({}, description='{name: CSS}: a page naming no posting, as '
                                                  '"page", the text of each')


class AtomDecoder(_Decoder):
    kind: Literal["atom"] = Field(description="An Atom feed's entries")
    entries: Paths = Field(("entries",), description="Where the decoded entries sit")


class HtmlDecoder(_Decoder):
    kind: Literal["html"] = Field(description="One entry per selected element (decode.elements)")
    entries: Paths = Field(("elements",), description="Where the decoded elements sit")
    select: Annotated[tuple[Template, ...], BeforeValidator(_listed)] = Field(
        min_length=1, description='CSS templates tried in order until one finds any; '
                                  '"$job_links": the careers-page reader (custom.read_page)')
    context: Literal["parent", "lines"] | tuple[Str, ...] | None = Field(
        None, description="The block around an element: its parent, the nearest of these "
                          "tags, or the nearest holding two lines")
    cells: dict[Str, Str] = Field({}, description="{name: CSS}: text found in the context")
    base: Template | None = Field(None, description="Makes hrefs absolute; default the page")
    selects: Bool = Field(False, description='Also the page\'s <select> fields, as "selects": '
                                             '[{name, options: [{value, label}]}]')


def _decoder_kind(v):
    """A decoder's `kind`: a dict naming none is JsonDecoder's default."""
    if isinstance(v, dict):
        return v["kind"] if "kind" in v else JsonDecoder.model_fields["kind"].default
    return getattr(v, "kind", None)


Decoder = Annotated[Union[Annotated[JsonDecoder, Tag("json")],
                          Annotated[JsonInHtmlDecoder, Tag("json_in_html")],
                          Annotated[JsonLdDecoder, Tag("jsonld")],
                          Annotated[AtomDecoder, Tag("atom")],
                          Annotated[HtmlDecoder, Tag("html")]],
                    Discriminator(_decoder_kind)]


class _Pager(_Workaround):
    WORKAROUNDS = ("ceiling",)
    size: Count = Field(description="Rows asked per page")
    pages: Count = Field(10, description="The most pages a walk reads")
    total: Grammar = Field(None, description="Names the board's total on the first page")
    ceiling: Count | None = Field(None, description="The most rows the server serves: a total "
                                                    "there ends nothing")
    declared: Grammar = Field(None, description="Names the last page, on every page")

    @property
    def stride(self):
        """Rows between one page's first row and the next's."""
        return self.size

    def number(self, n):
        """Page `n`'s (from 0) own number."""
        return n

    def page(self, n):
        """The "$page" value page `n` asks for."""
        return self.number(n)

    def offset(self, n, size):
        """The "$offset" value page `n` of `size` rows asks for."""
        return n * size


class OffsetPager(_Pager):
    kind: Literal["offset"] = Field(description='"$offset" steps a page')
    size: Count | None = Field(None, description="Rows asked per page; unset, the server sizes "
                                                 "its pages and the walk learns it (pager.walk)")
    start: Annotated[Int, Field(ge=0)] = Field(0, description="The first row's offset")

    def offset(self, n, size):
        return self.start + n * size


class OverlapPager(_Pager):
    WORKAROUNDS = ("ceiling", "step")
    kind: Literal["overlap"] = Field(description='"$offset" steps `step` < `size`: pages overlap')
    step: Count = Field(description="Rows each page steps")

    @model_validator(mode="after")
    def _overlaps(self):
        if self.step >= self.size:
            raise ValueError("pager: an overlap steps less than a page")
        return self

    @property
    def stride(self):
        return self.step

    def offset(self, n, size):
        return n * self.step


class PagePager(_Pager):
    WORKAROUNDS = ("ceiling", "bare_first")
    kind: Literal["page"] = Field(description='"$page" counts pages')
    size: Count | None = Field(None, description="Rows a page holds, when known")
    start: Annotated[Int, Field(ge=0)] = Field(0, description="The first page's number")
    bare_first: Bool = Field(False, description="The first page's request names no page")

    def number(self, n):
        return self.start + n

    def page(self, n):
        return None if n == 0 and self.bare_first else self.start + n


class CursorPager(_Pager):
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


class _Request(_Spec):
    url: Template = Field(description="The request's URL template")
    method: Literal["GET", "POST"] = Field("GET", description="The HTTP method")
    params: dict[Str, Str] | None = Field(None, description="Query parameters; a None one is "
                                                            "left off")
    json_: dict[Str, Any] | None = Field(None, alias="json", description="A JSON body template")
    headers: dict[Str, Str] = Field({}, description="Over the shared request headers")
    decoder: Decoder = Field(JsonDecoder(), description="Reads the response body")
    fields: dict[Str, Grammar] = Field({}, description="Row field (or internal _field) -> "
                                                       "field spec")

    @model_validator(mode="after")
    def _row_fields(self):
        extra = sorted(k for k in self.fields if k not in ROW_FIELDS and not k.startswith("_"))
        if extra:
            raise ValueError(f"unknown field(s) {extra}")
        return self


class Listing(_Request):
    probe_url: Template | None = Field(None, description="The URL a cheap read asks instead")
    pager: Pager | None = Field(None, description="How pages step; none reads one page")
    scope: Scope | None = Field(None, description="Narrows the listing to the locality")
    why: Why | None = Field(None, description='Why this fallback alternative exists: '
                                              '"reason, YYYY-MM"')


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
    fields: tuple[RowField, ...] = Field(("location",), description="The row fields the "
                                                                    "detail fills")
    why: Why = Field(description='Why the listing needs rescuing: "reason, YYYY-MM"')


class Rule(_Spec):
    when: Annotated[dict[Str, Any], AfterValidator(_condition)] = Field(
        description="A condition on the record")
    why: Grammar = Field(None, description="Names the reason")


Rules = Annotated[tuple[Rule, ...], BeforeValidator(_ruled)]


class Closure(_Workaround):
    WORKAROUNDS = ("url", "unmatched")
    via: Literal["detail", "listing", "page"] | None = Field(
        None, description="What judges a posting; default the detail where there is one, "
                          "else its page")
    url: Template | None = Field(None, description="Asked in place of the detail's URL")
    open: Rules = Field((), description="A condition, or rules, proving the posting live")
    closed: Rules = Field((), description="A condition, or rules, proving it closed")
    unmatched: Str | None = Field(None, description="The reason a readable answer neither "
                                                    "rule matches closes the posting")


def _default_of(model, key):
    """The default of `model`'s field named (or aliased) `key`; a marker
    no value equals when there is no such field."""
    info = next((f for n, f in model.model_fields.items() if key in (n, f.alias)), None)
    return info.get_default(call_default_factory=True) if info else object()


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
    handle: Handle = Field(Handle(), description="How a store row names the board")
    job_ref: JobRef | None = Field(None, description="Reads a stored posting URL")
    listing: tuple[Listing, ...] = Field(
        (), description="Alternatives tried in order until one yields a posting, each later "
                        "one taking what it does not set from the first")
    rescue: Rescue | None = Field(None, description="Fills vague listed rows from the detail")
    detail: Detail | None = Field(None, description="Reads one posting back")
    closure: Closure = Field(Closure(), description="Judges a stored posting open or closed")
    employer: Grammar = Field(None, description="Names the employer on a listing entry")
    unlocated: Literal["drop", "keep"] = Field(
        "drop", description="A location filter's verdict on a row naming no place")
    detect: tuple[Detect, ...] = Field((), description="How a URL or page names the board")
    canary: Canary | None = Field(None, description="The public board tools/check_boards.py "
                                                    "probes")
    discovery: Discovery = Field(Discovery(), description="How discovery finds and vets a board")

    @model_validator(mode="before")
    @classmethod
    def _alternatives(cls, data):
        """A lone listing as the one alternative; a later one completed
        from the first, a key it resets to its default left unset."""
        listing = data.get("listing") if isinstance(data, dict) else None
        if isinstance(listing, dict):
            return {**data, "listing": [listing]}
        if not (isinstance(listing, list) and listing
                and all(isinstance(alt, dict) for alt in listing)):
            return data
        first = listing[0]
        alts = [{**{k: v for k, v in first.items() if k not in alt},
                 **{k: v for k, v in alt.items()
                    if not (k in first and v == _default_of(Listing, k))}}
                for alt in listing[1:]]
        return {**data, "listing": [first, *alts]}

    @model_validator(mode="after")
    def _fallbacks_explained(self):
        for i, alt in enumerate(self.listing):
            if i == 0 and alt.why is not None:
                raise ValueError("listing[0].why: the first alternative is no fallback")
            if i and alt.why is None:
                raise ValueError(f'listing[{i}]: a fallback; say why ("reason, YYYY-MM")')
        return self

    @model_validator(mode="after")
    def _closure_servable(self):
        if self.via == "detail" and not self.detail:
            raise ValueError("closure.via detail needs a detail")
        if self.via == "listing" and (not self.listing or any(a.pager for a in self.listing)):
            raise ValueError("closure.via listing reads one page: its listing cannot page")
        return self

    @model_validator(mode="after")
    def _rescue_servable(self):
        scoped = self.listing and self.listing[0].scope
        if self.rescue and not (self.detail and (scoped or self.rescue.when == "always")):
            raise ValueError("rescue: a detail, and a scoped listing unless `when` is always")
        return self

    @property
    def via(self):
        """What judges a stored posting (`closure.via`, resolved)."""
        return self.closure.via or self.default_via

    @property
    def default_via(self):
        """`closure.via` unless set: the detail where there is one, else the page."""
        return "detail" if self.detail else "page"


def parse(name, raw):
    """`raw`, a `config.BOARDS` entry, as a BoardSpec; ValueError naming
    `name` and every broken key's path when it breaks the schema."""
    try:
        return BoardSpec.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{name}: {e}") from None


def walk(model, path=""):
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
