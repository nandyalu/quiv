"""Generate docs/llms-full.txt — a single-file concatenation of the user-facing docs.

Run from the repo root before `zensical build`; the output lands in the docs dir
so the site build publishes it at /llms-full.txt (llms.txt convention).
"""

from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

# User-facing pages in reading order; release-notes (generated) and roadmap excluded.
PAGES = [
    "index.md",
    "getting-started.md",
    "bigger-applications.md",
    "api.md",
    "architecture.md",
    "progress-callbacks.md",
    "run-on-main.md",
    "event-listeners.md",
    "cancellation.md",
    "exceptions.md",
    "testing.md",
]


def main() -> None:
    parts = [
        "# quiv — full documentation\n\n"
        "> Concatenated from https://nandyalu.github.io/quiv for LLM consumption.\n"
        "> Index: https://nandyalu.github.io/quiv/llms.txt\n"
    ]
    for page in PAGES:
        text = (DOCS / page).read_text(encoding="utf-8")
        parts.append(f"\n\n---\n<!-- source: docs/{page} -->\n\n{text}")
    out = DOCS / "llms-full.txt"
    out.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
