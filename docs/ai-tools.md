# Using quiv with AI coding tools

quiv ships several entry points so AI assistants (Claude Code, Cursor, Copilot, and friends) can learn the library accurately instead of guessing.

## Bundled agent guide (zero setup)

Every quiv install includes a condensed, agent-oriented reference at `quiv/AGENTS.md` inside the package — it lands in your environment's `site-packages` alongside the code. AI tools that explore installed dependencies will find it there; you can also point your assistant at it directly:

```bash
python -c "import quiv, pathlib; print(pathlib.Path(quiv.__file__).parent / 'AGENTS.md')"
```

A useful line for your project's `AGENTS.md` / `CLAUDE.md`:

> This project uses quiv for background scheduling. Before writing quiv code,
> read the packaged guide at `<site-packages>/quiv/AGENTS.md`.

## llms.txt

The documentation site publishes the [llms.txt](https://llmstxt.org/) convention:

- [`https://nandyalu.github.io/quiv/llms.txt`](https://nandyalu.github.io/quiv/llms.txt) — index with a summary and links to every docs page as raw Markdown
- [`https://nandyalu.github.io/quiv/llms-full.txt`](https://nandyalu.github.io/quiv/llms-full.txt) — the full documentation concatenated into one file

Tools that support llms.txt can fetch these directly; for others, paste the URL into the chat.

## Claude Code plugin

The quiv repository doubles as a Claude Code plugin marketplace shipping a `quiv` skill — usage patterns, handler injection rules, cancellation semantics, FastAPI wiring, and common mistakes. Claude loads it automatically whenever a conversation touches quiv.

Install once per machine:

```
/plugin marketplace add nandyalu/quiv
/plugin install quiv@quiv
```

The skill tracks the repository, so `/plugin update quiv` picks up the latest guidance.
