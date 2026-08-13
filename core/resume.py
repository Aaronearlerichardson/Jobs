"""Résumé text extraction + caching for per-job fit scoring."""

import re
import zipfile

import config

_cache = None


def _extract_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    lines = []
    for para in re.split(r"</w:p>", xml):
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        line = re.sub(r"<[^>]+>", "", "".join(runs)).strip()
        if line:
            lines.append(line)
    # unescape the few XML entities that survive the run extraction
    text = "\n".join(lines)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(a, b)
    return text


def resume_text():
    """
    Return the résumé as plain text (cached). Supports .docx and plain
    .txt/.md. Returns "" if the configured RESUME_PATH is missing.
    """
    global _cache
    if _cache is not None:
        return _cache
    path = config.RESUME_PATH
    try:
        if str(path).lower().endswith(".docx"):
            _cache = _extract_docx(path)
        else:
            _cache = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"  [!] Résumé not found at {path} — fit scoring disabled.")
        _cache = ""
    except Exception as e:
        print(f"  [!] Résumé read failed ({e}) — fit scoring disabled.")
        _cache = ""
    return _cache
