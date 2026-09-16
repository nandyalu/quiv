# Using quiv with AI coding tools

quiv ships several entry points, so that an AI assistant such as Claude Code, Cursor, or Copilot can read how the library works instead of guessing.

## Bundled agent guide (no setup)

Every installation of quiv includes a short reference for agents at `quiv/AGENTS.md`, inside the package. It lands in the `site-packages` directory of your environment, next to the code. An AI tool that reads installed dependencies finds it there.

To point your assistant at the file directly, print its path:

```bash
python -c "import quiv, pathlib; print(pathlib.Path(quiv.__file__).parent / 'AGENTS.md')"
```

Add this line to the `AGENTS.md` or `CLAUDE.md` file of your own project:

> This project uses quiv for background scheduling. Before writing quiv code,
> read the packaged guide at `<site-packages>/quiv/AGENTS.md`.

## llms.txt

The documentation site follows the [llms.txt](https://llmstxt.org/) convention and publishes two files:

- [`https://nandyalu.github.io/quiv/llms.txt`](https://nandyalu.github.io/quiv/llms.txt) — an index with a summary and a link to every documentation page as raw Markdown.
- [`https://nandyalu.github.io/quiv/llms-full.txt`](https://nandyalu.github.io/quiv/llms-full.txt) — the full documentation in one file.

A tool that supports llms.txt reads these files directly. For a tool that does not, paste the URL into the chat.

## Claude Code plugin

The quiv repository is also a Claude Code plugin marketplace. It ships a `quiv` skill that covers the usual patterns, the rules for handler injection, how cancellation behaves, how to wire quiv into FastAPI, and the mistakes that people make most often. Claude loads the skill whenever a conversation reaches quiv.

Install it once for each machine:

```
/plugin marketplace add nandyalu/quiv
/plugin install quiv@quiv
```

The skill follows the repository, so `/plugin update quiv` fetches the current version.
