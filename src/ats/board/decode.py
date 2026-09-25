"""Response bodies as data: the decoders a `config.BOARDS` spec names
(`spec.Decoder`), and a decoded payload's entries and a detail's record.
"""

import functools
import json
import re
from urllib.parse import urljoin

from cssselect import HTMLTranslator
from lxml import etree

from src.net.util import node_text, parse_markup, xpath
from . import custom, fields, jsonld

_CSS = HTMLTranslator()


def decode(dec, text, parts, url, area=None, hop=True):
    """A non-JSON response body (to `url`) as data; None when it holds
    none. Raises ValueError when embedded JSON will not parse. `area` and
    `hop` reach the careers-page reader (`custom.read_page`)."""
    kind = dec.kind
    if kind == "json_in_html":
        m = re.search(dec.regex, text)
        return json.JSONDecoder().raw_decode(text, m.end())[0] if m else None
    if kind == "jsonld":
        found = jsonld.postings(text, url)
        if found or not dec.cells:
            return {"postings": found}
        return {"postings": [], "page": _cells(parse_markup(text, url=url), dec.cells)}
    if kind == "atom":
        return {"entries": _atom(text, url)}
    tree = parse_markup(text, url=url)
    if dec.select == ("$job_links",):
        return custom.read_page(tree, url, area, hop)
    payload = {"elements": elements(dec, tree, parts, url), "page": text}
    if dec.selects:
        payload["selects"] = _selects(tree)
    return payload


def entries(payload, dec):
    """The postings (dicts) in a payload decoded by `dec`: the first of its
    entry paths holding a list; a wrong shape is []."""
    return [e for e in first_path(payload, dec.entries, list) or [] if isinstance(e, dict)]


def record(payload, detail):
    """A detail answer's record: the first dict at the detail's `record`
    paths, by default its decoder's first entry; None when there is none."""
    if not payload:
        return None
    return first_path(payload, (detail.decoder.first,) if detail.record is None else detail.record,
                      dict)


def _atom(text, url=""):
    """An Atom feed's entries, each an `_xml_record` carrying its feed's own
    elements under "feed" (an entry inherits its feed's metadata).

    >>> feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><title>State U: All Jobs</title>'
    ...         '<entry><title>Chemist</title><link href="https://x.test/postings/7"/>'
    ...         '<author><name>Chemistry</name></author></entry></feed>')
    >>> _atom(feed)
    [{'title': 'Chemist', 'link': '', 'link@href': 'https://x.test/postings/7', 'author': {'name': 'Chemistry'}, 'feed': {'title': 'State U: All Jobs'}}]
    """
    root = parse_markup(text, xml=True, url=url)
    feed = _xml_record(next(iter(xpath("descendant-or-self::*[local-name()='feed']")(root)), root),
                       skip="entry")
    return [{**_xml_record(e), "feed": feed} for e in xpath("//*[local-name()='entry']")(root)]


def _xml_record(el, skip=None):
    """An XML element's children as a dict, the first of each local name
    (but `skip`): a child holding elements as its own record, else its
    text; each attribute as "<name>@<attribute>"."""
    out = {}
    for child in el.iterchildren(etree.Element):
        name = etree.QName(child).localname
        if name in out or name == skip:
            continue
        out[name] = (_xml_record(child) if next(child.iterchildren(etree.Element), None) is not None
                     else node_text(child))
        for attr, v in child.attrib.items():
            out[f"{name}@{etree.QName(attr).localname}"] = v
    return out


def _selects(tree):
    """The page's <select> fields: [{"name", "options": [{"value", "label"}]}].

    >>> _selects(parse_markup('<select name="loc"><option value="7-Durham"> Durham, NC</select>'))
    [{'name': 'loc', 'options': [{'value': '7-Durham', 'label': 'Durham, NC'}]}]
    """
    return [{"name": s.get("name") or "",
             "options": [{"value": o.get("value") or "", "label": node_text(o)}
                         for o in s.iterdescendants("option")]}
            for s in tree.iter("select")]


def elements(dec, tree, parts, url):
    """One entry per element the html decoder's `select` finds (a CSS
    template over the handle `parts`, or a list tried in order until one
    finds any): its `text`, its `raw` text (unstripped, line breaks
    kept), `href` and `url` (the href made absolute against `base`,
    default the page). With a `context`, also that element's text
    (`_context`), its `lines` where the context reads them, and each of
    `cells`, {name: CSS}, the text of the first match inside it (None
    when none).

    >>> from .spec import HtmlDecoder
    >>> page = ('<ul><li><a class="j" href="/acme/job/1">Data Engineer</a>'
    ...         '<p class="loc">Durham, NC</p></li></ul>')
    >>> dec = HtmlDecoder(kind="html", select="a.j[href*='/{slug}/']", context=["li"],
    ...                   cells={"loc": ".loc"})
    >>> elements(dec, parse_markup(page), {"slug": "acme"}, "https://x.test/acme")
    [{'text': 'Data Engineer', 'raw': 'Data Engineer', 'href': '/acme/job/1', 'url': 'https://x.test/acme/job/1', 'context': 'Data Engineer Durham, NC', 'loc': 'Durham, NC'}]
    """
    found = []
    for sel in dec.select:
        found = css(fields.fmt(sel, parts.get))(tree)
        if found:
            break
    base = fields.fmt(dec.base, parts.get) if dec.base else url
    out = []
    for el in found:
        href = el.get("href") or ""
        e = {"text": node_text(el), "raw": node_text(el, " ", strip=False), "href": href,
             "url": urljoin(base, href) if href else ""}
        if dec.context is not None or dec.cells:
            ctx, lines = _context(el, dec.context or "parent")
            e["context"] = node_text(ctx) if ctx is not None else ""
            if lines is not None:
                e["lines"] = lines
            e.update(_cells(ctx, dec.cells))
        out.append(e)
    return out


def _cells(node, cells):
    """{name: the text of `cells[name]`'s first match inside `node`}, None
    where it has none."""
    out = {}
    for name, sel in cells.items():
        hit = css(sel, relative=True)(node) if node is not None else []
        out[name] = node_text(hit[0]) if hit else None
    return out


@functools.cache
def _css_xpath(selector, relative):
    return _CSS.css_to_xpath(selector, prefix="descendant::" if relative else "descendant-or-self::")


def css(selector, relative=False):
    """`selector`, CSS in cssselect's HTML dialect, as this thread's
    compiled XPath (`net.util.xpath`): matching the element it runs on
    and everything below (from the root, the whole document), or only
    what is below when `relative`. Raises cssselect's SelectorError on CSS
    the dialect cannot read.

    >>> page = parse_markup('<ul><li><a href="/job/1">A</a><p class="loc">Durham</p></li>'
    ...                     '<li><a href="/job/2">B</a></li></ul>')
    >>> [a.get("href") for a in css("li:has(.loc) a[href*='/job/']")(page)]
    ['/job/1']
    >>> len(css("li")(page.find(".//li"))), css("li", relative=True)(page.find(".//li"))
    (1, [])
    """
    return xpath(_css_xpath(selector, relative))


def _context(el, how):
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
        node, lines = el.getparent(), []
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


def first_path(payload, wanted, kind):
    """The first value of type `kind` at one of the paths `wanted` in
    `payload`, or None."""
    for p in wanted:
        v = fields.path(payload, p)
        if isinstance(v, kind):
            return v
    return None


def unwrap(v, key):
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
