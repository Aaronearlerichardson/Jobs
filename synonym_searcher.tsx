import { useState, useRef } from "react";

const SECTIONS = ["synonyms", "semantically similar", "associated words"];

export default function SynonymSearcher() {
  const [term, setTerm] = useState("");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState([]);
  const inputRef = useRef();

  async function search(word) {
    const q = (word || term).trim();
    if (!q) return;
    setLoading(true);
    setResults(null);
    setQuery(q);
    setHistory(h => [q, ...h.filter(x => x !== q)].slice(0, 8));

    try {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "claude-sonnet-4-20250514",
          max_tokens: 1000,
          system: `You are a thesaurus engine. Given a word or phrase, return a JSON object with exactly three keys:
"synonyms": up to 12 direct substitutes (same meaning, interchangeable),
"similar": up to 12 semantically related words (same domain/concept, looser match),
"associated": up to 12 words strongly evoked or co-occurring with the input.
Each value is an array of plain strings. Return ONLY valid JSON, no markdown, no explanation.`,
          messages: [{ role: "user", content: q }]
        })
      });
      const data = await res.json();
      const text = data.content?.find(b => b.type === "text")?.text || "{}";
      const parsed = JSON.parse(text.replace(/```json|```/g, "").trim());
      setResults({
        synonyms: parsed.synonyms || [],
        similar: parsed.similar || [],
        associated: parsed.associated || []
      });
    } catch (e) {
      setResults({ error: true });
    }
    setLoading(false);
  }

  function handleKey(e) {
    if (e.key === "Enter") search();
  }

  function clickWord(w) {
    setTerm(w);
    search(w);
  }

  const sections = results && !results.error ? [
    { label: "Synonyms", key: "synonyms", words: results.synonyms },
    { label: "Semantically similar", key: "similar", words: results.similar },
    { label: "Associated words", key: "associated", words: results.associated },
  ] : [];

  return (
    <div style={{ padding: "1.5rem 0", fontFamily: "var(--font-sans)" }}>
      <div style={{ display: "flex", gap: 8, marginBottom: "1rem" }}>
        <input
          ref={inputRef}
          type="text"
          value={term}
          onChange={e => setTerm(e.target.value)}
          onKeyDown={handleKey}
          placeholder="Enter a word or phrase…"
          autoFocus
          style={{ flex: 1 }}
        />
        <button onClick={() => search()} disabled={loading} style={{ padding: "0 1.25rem", cursor: loading ? "default" : "pointer" }}>
          {loading ? "…" : "Search"}
        </button>
      </div>

      {history.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: "1rem" }}>
          {history.map(w => (
            <button key={w} onClick={() => { setTerm(w); search(w); }}
              style={{ fontSize: 12, padding: "3px 10px", borderRadius: 999, border: "0.5px solid var(--color-border-secondary)", color: "var(--color-text-secondary)", cursor: "pointer", background: "transparent" }}>
              {w}
            </button>
          ))}
        </div>
      )}

      {query && (
        <div style={{ fontSize: 22, fontWeight: 500, color: "var(--color-text-primary)", marginBottom: "1rem" }}>
          {query}
        </div>
      )}

      {loading && (
        <div style={{ fontSize: 13, color: "var(--color-text-secondary)" }}>Looking up words…</div>
      )}

      {results?.error && (
        <div style={{ fontSize: 13, color: "var(--color-text-danger)" }}>Something went wrong. Try again.</div>
      )}

      {sections.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
          {sections.map(sec => (
            <div key={sec.key} style={{ background: "var(--color-background-primary)", border: "0.5px solid var(--color-border-tertiary)", borderRadius: "var(--border-radius-lg)", padding: "1rem 1.25rem" }}>
              <div style={{ fontSize: 11, fontWeight: 500, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--color-text-tertiary)", marginBottom: 10 }}>
                {sec.label}
              </div>
              {sec.words.length === 0
                ? <div style={{ fontSize: 13, color: "var(--color-text-tertiary)", fontStyle: "italic" }}>No results</div>
                : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {sec.words.map(w => (
                      <button key={w} onClick={() => clickWord(w)}
                        style={{ fontSize: 14, color: "var(--color-text-info)", cursor: "pointer", background: "none", border: "none", padding: 0, textAlign: "left", fontFamily: "var(--font-sans)" }}>
                        {w}
                      </button>
                    ))}
                  </div>
                )
              }
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
