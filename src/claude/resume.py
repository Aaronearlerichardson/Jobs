"""Résumé text extraction + caching for per-job fit scoring."""

import re
import zipfile

from src import config, runstate


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


def _read_resume():
    """The résumé as plain text: .docx, or plain .txt/.md; "" (said) when
    the configured RESUME_PATH is missing or unreadable."""
    path = config.RESUME_PATH
    try:
        if str(path).lower().endswith(".docx"):
            return _extract_docx(path)
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"  [!] Résumé not found at {path} — fit scoring disabled.")
    except Exception as e:
        print(f"  [!] Résumé read failed ({e}) — fit scoring disabled.")
    return ""


#: The résumé as plain text, read once per run.
resume_text = runstate.per_run(_read_resume)
