"""Response bodies as data: the decoders a `config.BOARDS` spec names
(`spec.Decoder`), and a decoded payload's entries and a detail's record.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from lxml import etree

from src.match.locality import LocationRE
from src.net.util import JSON, css, first, named, node_text, parse_markup, xpath
from . import custom, fields, jsonld
from .spec import (AtomDecoder, Decoder, Detail, HtmlDecoder, JsonInHtmlDecoder,
                   JsonLdDecoder)


def decode(dec: JsonInHtmlDecoder | JsonLdDecoder | AtomDecoder | HtmlDecoder, text: str,
           parts: dict[str, str], url: str, area: LocationRE | None = None,
           hop: bool = True) -> tuple[JSON, str | None]:
    """A non-JSON response body (to `url`) as (data, hop): data None when it
    holds none; hop the openings page to read in its place, when the
    careers-page reader (`custom.read_page`, given `area` and `hop`) names
    one. Raises ValueError when embedded JSON will not parse.

    >>> dec = HtmlDecoder(kind="html", select="a", selects=True)
    >>> decode(dec, '<select name="loc"><option value="7-Durham"> Durham, NC</select>', {},
    ...        "https://x.test")[0]["selects"]
    [{'name': 'loc', 'options': [{'value': '7-Durham', 'label': 'Durham, NC'}]}]
    """
    if dec.kind == "json_in_html":
        m = re.search(dec.regex, text)
        return (json.JSONDecoder().raw_decode(text, m.end())[0] if m else None), None
    if dec.kind == "jsonld":
        found = jsonld.postings(text, url)
        if found or not dec.cells:
            return {"postings": found}, None
        return {"postings": [], "page": _cells(parse_markup(text, url=url), dec.cells)}, None
    if dec.kind == "atom":
        return {"entries": _atom(text, url)}, None
    tree = parse_markup(text, url=url)
    if dec.select == ("$job_links",):
        page = custom.read_page(tree, url, area, hop)
        return (None, page["hop"]) if "hop" in page else (page, None)
    payload: dict[str, JSON] = {"elements": elements(dec, tree, parts, url), "page": text}
    if dec.selects:
        payload["selects"] = [{"name": s.get("name") or "",
                               "options": [{"value": o.get("value") or "", "label": node_text(o)}
                                           for o in s.iterdescendants("option")]}
                              for s in tree.iter("select")]
    return payload, None


def entries(payload: JSON, dec: Decoder) -> list[dict[str, JSON]]:
    """The postings (dicts) in a payload decoded by `dec`: the first of its
    entry paths holding a list; a wrong shape is []."""
    for p in dec.entries:
        if isinstance(found := fields.path(payload, p), list):
            return [e for e in found if isinstance(e, dict)]
    return []


def record(payload: JSON, detail: Detail) -> dict[str, JSON] | None:
    """A detail answer's record: the first dict at the detail's `record`
    paths, by default its decoder's first entry; None when there is none."""
    if not payload:
        return None
    for p in (detail.decoder.first,) if detail.record is None else detail.record:
        if isinstance(found := fields.path(payload, p), dict):
            return found
    return None


def _atom(text: str, url: str = "") -> list[dict[str, JSON]]:
    """An Atom feed's entries, each an `_xml_record` carrying its feed's own
    elements under "feed" (an entry inherits its feed's metadata).

    >>> feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><title>State U: All Jobs</title>'
    ...         '<entry><title>Chemist</title><link href="https://x.test/postings/7"/>'
    ...         '<author><name>Chemistry</name></author></entry></feed>')
    >>> _atom(feed)
    [{'title': 'Chemist', 'link': '', 'link@href': 'https://x.test/postings/7', 'author': {'name': 'Chemistry'}, 'feed': {'title': 'State U: All Jobs'}}]
    """
    root = parse_markup(text, xml=True, url=url)
    feed = named(root, "feed", one=True)
    feed = _xml_record(root if feed is None else feed, skip="entry")
    return [{**_xml_record(e), "feed": feed} for e in named(root, "entry")]


def _xml_record(el: etree._Element, skip: str | None = None) -> dict[str, JSON]:
    """An XML element's children as a dict, the first of each local name
    (but `skip`): a child holding elements as its own record, else its
    text; each attribute as "<name>@<attribute>"."""
    out: dict[str, JSON] = {}
    for child in el.iterchildren(etree.Element):
        name = etree.QName(child).localname
        if name in out or name == skip:
            continue
        out[name] = (_xml_record(child) if next(child.iterchildren(etree.Element), None) is not None
                     else node_text(child))
        for attr, v in child.attrib.items():
            out[f"{name}@{etree.QName(attr).localname}"] = v
    return out


def elements(dec: HtmlDecoder, tree: etree._Element, parts: dict[str, str],
             url: str) -> list[dict[str, JSON]]:
    """One entry per element the html decoder's `select` finds (a CSS
    template over the handle `parts`, or a list tried in order until one
    finds any): its `text`, its `raw` text (unstripped, line breaks
    kept), `href` and `url` (the href made absolute against `base`,
    default the page). With a `context`, also that element's text
    (`_context`), its `lines` where the context reads them, and each of
    `cells`, {name: CSS}, the text of the first match inside it (None
    when none).

    >>> page = ('<ul><li><a class="j" href="/acme/job/1">Data Engineer</a>'
    ...         '<p class="loc">Durham, NC</p></li></ul>')
    >>> dec = HtmlDecoder(kind="html", select="a.j[href*='/{slug}/']", context=["li"],
    ...                   cells={"loc": ".loc"})
    >>> elements(dec, parse_markup(page), {"slug": "acme"}, "https://x.test/acme")
    [{'text': 'Data Engineer', 'raw': 'Data Engineer', 'href': '/acme/job/1', 'url': 'https://x.test/acme/job/1', 'context': 'Data Engineer Durham, NC', 'loc': 'Durham, NC'}]
    """
    found: list[etree._Element] = []
    for sel in dec.select:
        found = xpath(css(fields.fmt(sel, parts.get)))(tree)
        if found:
            break
    base = fields.fmt(dec.base, parts.get) if dec.base else url
    out: list[dict[str, JSON]] = []
    for el in found:
        href = el.get("href") or ""
        e: dict[str, JSON] = {"text": node_text(el), "raw": node_text(el, " ", strip=False),
                             "href": href, "url": urljoin(base, href) if href else ""}
        if dec.context is not None or dec.cells:
            ctx, lines = _context(el, dec.context or "parent")
            e["context"] = node_text(ctx) if ctx is not None else ""
            if lines is not None:
                e["lines"] = lines
            e.update(_cells(ctx, dec.cells))
        out.append(e)
    return out


def _cells(node: etree._Element | None, cells: dict[str, str]) -> dict[str, str | None]:
    """{name: the text of `cells[name]`'s first match inside `node`}, None
    where it has none."""
    out: dict[str, str | None] = {}
    for name, sel in cells.items():
        hit = first(css(sel, relative=True), node) if node is not None else None
        out[name] = None if hit is None else node_text(hit)
    return out


def _context(el: etree._Element,
             how: str | tuple[str, ...]) -> tuple[etree._Element | None, list[str] | None]:
    """(element, lines) around a matched element: its parent ("parent");
    the nearest ancestor of the first of a list of tags that has one; or
    ("lines") the nearest ancestor, at most eight up, whose text holds two
    lines longer than three characters, with those lines (else None).

    >>> a = parse_markup("<li><div><p>Research</p>\\n<p><a>Data Engineer</a></p>\\n"
    ...                  "<p>Durham, NC</p></div></li>").find(".//a")
    >>> _context(a, "lines")[1], _context(a, ("li", "div"))[0].tag
    (['Research', 'Data Engineer', 'Durham, NC'], 'li')
    """
    if how == "lines":
        node = el.getparent()
        lines: list[str] = []
        for _ in range(8):
            if node is None:
                break
            lines = [ln.strip() for ln in node_text(node, "\n", strip=False).strip().split("\n")
                     if len(ln.strip()) > 3]
            if len(lines) >= 2:
                break
            node = node.getparent()
        return node, lines
    if isinstance(how, tuple):
        return next((p for p in (next(el.iterancestors(t), None) for t in how)
                     if p is not None), None), None
    return el.getparent(), None


def unwrap(v: JSON, key: str) -> JSON:
    """`v` with every dict holding `key` replaced by that key's value: a
    decoder's `values`, for a payload that wraps each value in a dict.

    >>> unwrap({"f": {"A": {"value": 1, "size": 3}}, "n": [{"value": "x"}]}, "value")
    {'f': {'A': 1}, 'n': ['x']}
    """
    if isinstance(v, dict):
        return v[key] if key in v else {k: unwrap(x, key) for k, x in v.items()}
    if isinstance(v, list):
        return [unwrap(x, key) for x in v]
    return v
