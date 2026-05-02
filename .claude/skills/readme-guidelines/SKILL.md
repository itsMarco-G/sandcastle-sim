---
description: Guidelines for writing READMEs in this project. Use when creating, updating, or improving any README.md file.
---

# README skill

## Philosophy

A README is the project's front door. The job is to get someone from zero to working as fast
as possible, then get out of the way. Every word earns its place.

**Terse section titles. Warmth in the prose underneath.**
Headings are labels — devs scan them, they read captions.
The human tone lives in the 1–3 sentences below each heading, not in the heading itself.

**The README has two audiences: humans and agents.**
Both read it. Humans skim for narrative; agents scan for entry points, file paths, and
commands they can execute. A well-structured README serves both without compromise. For
agent-facing projects (MCP servers, SDKs, tools built to be called by AI), an `AGENTS.md`
file at the repo root is strongly recommended — it gives agents a dedicated, optimised
surface without cluttering the README for human readers. The README should always link
to it explicitly.

---

## Tone

Modelled on Claude Code / Anthropic docs. The voice to match:

- **Direct without being cold.** State what a thing does, then stop.
- **Helpful dependable friend** — not a corporate doc, not a hype reel. Talks to the reader
  like someone who knows the codebase and wants them to succeed.
- **Active, not instructional.** Prefer "run this" over "you should run this". Avoid overusing "you".
- **No filler.** Every sentence either teaches something or moves the reader forward.
- **Sentence case everywhere** — headings, badges, captions, inline code comments.
- No emoji in headings. Emoji in prose only if the project already uses them.
- No exclamation marks.
- **No em-dashes.** Use a full stop, semicolon, colon, or restructure the sentence instead.

### Tone examples (locked — use these as the reference voice)

These are the exact patterns to match. When writing prose sections, check the output
against these before finalising.

---

**Intro:**
> Lightweight Python library for building composable data pipelines. Handles the wiring
> between steps so the logic stays where it belongs. Works with lists, generators, and
> async streams.

What works: concrete nouns, no adjective inflation, ends on a practical detail not a promise.

---

**Quickstart caption:**
> Works on any iterable. From here, swap in real steps or look at `docs/api.md` for the
> full list of built-ins — there are a few useful ones already in the box.

What works: points forward without being pushy, sounds like a friend who already knows
the codebase, trusts the reader to explore.

---

**Customize opener:**
> The repo is set up to be forkable. Handlers cover the logic, config covers the knobs,
> plugins cover the escape hatches. Here's where each one lives.

What works: orients quickly, uses threes naturally, "here's where each one lives" is
friendly without being performative.

---

**What bad looks like — never write this:**
> This powerful library will help you easily build flexible data pipelines! It's designed
> to be developer-friendly and highly customizable. You'll love how simple it is to get started!

Problems: adjective-heavy, no concrete detail, performed enthusiasm, overuses "you".

---

## Section order (canonical)

Produce sections in this order. Skip any that don't apply. Add a ToC only when 6 or more
sections are present (auto-generate GitHub anchor links).

```
1. Title + badges
2. Intro (relaxed length — see below)
3. Visual (screenshot > SVG diagram > nothing)
4. Install
5. Quickstart
6. Usage (only if meaningfully distinct patterns exist beyond quickstart — see below)
7. Connect your agent / integration (if relevant)
8. Customize
9. Read more
```

For agent-facing projects, also include at the end of the Quickstart or as a standalone
callout:

```markdown
Using an AI coding agent? Start by reading [AGENTS.md](AGENTS.md).
```

---

## Section-by-section rules

### 1. Title + badges

- `# projectname` — lowercase unless the project name is stylised
- Badge row immediately below the title, always included
- **Python badge set (required):** build status, PyPI version, license, Python versions
- Optional: coverage, downloads — only if the info is available

```markdown
![build](https://img.shields.io/github/actions/workflow/status/org/projectname/ci.yml)
![pypi](https://img.shields.io/pypi/v/projectname)
![license](https://img.shields.io/pypi/l/projectname)
![python](https://img.shields.io/pypi/pyversions/projectname)
```

### 2. Intro

No hard sentence limit. The intro must address two things:

- **Why it exists** — what problem it solves, what alternative it replaces
- **Who it's for** — be specific: "for developers writing agents with the Anthropic SDK",
  not "for developers"

Keep it tight but don't sacrifice either. Two short paragraphs is fine if the project
warrants it. No bullet lists here. No feature enumeration.

Do not start with "This is a..." — lead with what the thing does or the problem it solves.

For flavour when describing what the project simulates or includes, name 2-3 representative
examples rather than listing everything. Choose ones that show range or hint at the
interesting behaviour. "Lights, a robot vacuum, and sensors that fire on their own" is
better than "lights, locks, blinds, climate, sensors, vacuum, power meter".

### 3. Visual

Priority order:

1. **Real screenshot or demo GIF** — if one exists or a path is provided, always embed it.
   Use an image file reference: `![architecture](docs/architecture.png)`
   Never try to recreate a real screenshot as an SVG.

2. **SVG architecture diagram** — if no screenshot exists, generate an inline SVG that shows
   the key components and their relationships. Show the developer interaction loop where
   relevant: input goes in, agent reasons, MCP calls go out, something changes, developer
   sees it. Keep to 5-7 nodes. Use subgraphs or rows to group related components.
   Prefer this over Mermaid — SVG renders reliably everywhere; Mermaid does not.

3. **Skip** — if neither applies (pure utility library, trivial architecture), omit entirely.
   Never pad with a placeholder.

### 4. Install

#### Prerequisites subsection

Always include a `### Prerequisites` subsection. Format as bullets.

Include:
- Runtime version requirements
- System dependencies (Docker, native libs, CLI tools)
- **Hardware compatibility** — name tested platforms specifically:
  e.g. "Mac (Apple Silicon), Linux, Raspberry Pi 4/5. Windows not yet tested."
- Do NOT list "A browser" — this is never a prerequisite

```markdown
### Prerequisites

- [Docker](https://docs.docker.com/compose/) with Compose v2
- Python >= 3.10
- Tested on Mac (Apple Silicon), Linux, and Raspberry Pi 4/5. Windows not yet tested.
```

#### Dependency explanation rule

Every dependency must answer: what is it, why does this project need it, is it mandatory
or optional? Universal tools (Docker, pip) need no explanation. Non-obvious or situational
dependencies must be explained **inline at the point of use**, not just listed.

- If a dependency is the default/demo path but not required: say so when it first appears.
  Example: "The built-in demo path uses Ollama. Any MCP client works — Ollama is just
  the quickest way to get something running."
- If a dependency is optional: mark it clearly and explain when you'd need it.

Never leave a reader wondering "wait, do I actually need this?"

#### Command blocks

**Slow commands get their own block. Instant commands can share a block.**

A command is slow if it involves: network I/O (cloning, pulling a model, downloading
packages), Docker startup, or anything that takes more than a few seconds.

```sh
git clone git@github.com:org/projectname.git
```

```sh
cd projectname
pip install -e .
```

`git clone` is slow (network). `cd` and `pip install` finish instantly once it's done.

**Python install conventions — in priority order:**

1. `pip` — always show, universal baseline
2. `uv` — show if the project uses `pyproject.toml` or targets modern tooling
3. `pipx` — show only for CLI tools meant to be installed globally

For dev/source installs, show separately:
```sh
pip install -e ".[dev]"
```

### 5. Quickstart

The single most important section. Goal: first success in under 2 minutes.

- Show the minimal working example
- Use ` ```python ` for code, ` ```sh ` for shell commands
- **Slow commands get their own block** (see Install rules above)
- If a dependency is optional or situational, say so in the caption before showing it
- 1-2 sentence caption after the code. Match the locked quickstart tone example above.
- End with a pointer to `--help` or docs for next steps — don't enumerate every flag here

### 6. Usage

**Only include this section if** there are meaningfully distinct usage patterns beyond what
the quickstart already shows. If `--help` covers the command surface, point there instead
of duplicating it.

If included: 2-4 examples max, each with a brief inline comment or 1-sentence caption.
If there are more than 4 meaningful patterns, link to `docs/api.md` instead.

Never use this section as a man page. That's what `--help` is for.

### 7. Connect / integration (project-specific)

For projects that expose an API, MCP server, or SDK integration point: show the minimal
connection example in code. Then link to the full integration guide.

This section is for the agent/client developer audience, not the contributor audience.
Keep it short — one code block, one link.

### 8. Customize

Purpose: map the codebase for someone who wants to extend or adapt it.

Format:
- Opening paragraph: match the locked customize tone example. Orient quickly, name the
  main extension points, end with "here's where each one lives" or similar.
- Named list of key files/directories with one-line descriptions
- Closing link to the detailed guide: `[docs/customising.md](docs/customising.md)`

The linked file is the deep-dive. This section is the map — don't inline a walkthrough.

**Python-idiomatic file list:**
```
- `src/projectname/handlers/` — one module per operation; filename becomes the key
- `src/projectname/config.py` — dataclass config; all fields documented inline
- `src/projectname/plugins/` — optional extensions; auto-discovered via entry points
- `pyproject.toml` — optional extras: `.[dev]`, `.[async]`
```

Use ` : ` as the separator between path and description. No em-dashes.

### 9. Read more

Links only to other markdown files in the repo. No external links unless an official
docs site exists. Only list files that exist or will be created — no placeholders.

Use ` : ` as the separator. No em-dashes.

```
- [AGENTS.md](AGENTS.md) : orientation for AI coding agents
- [docs/api.md](docs/api.md) : full API reference
- [CONTRIBUTING.md](CONTRIBUTING.md) : how to contribute
- [CHANGELOG.md](CHANGELOG.md)
```

**CONTRIBUTING.md belongs at the repo root, not in `docs/`.** GitHub automatically
surfaces it on new issues and pull requests. `CONTRIBUTING.md` (root) is the convention
followed by Reachy Mini, Ultralytics, and most well-maintained open source projects.

### AGENTS.md (recommended for agent-facing projects)

For any project designed to be used by, integrated with, or built on by AI agents,
`AGENTS.md` at the repo root is a first-class deliverable — not an afterthought.

**What it should contain:**
- How to connect to the project's API/MCP/SDK in one working code block
- Key file paths agents will need to navigate or modify
- Patterns and conventions the codebase follows (naming, structure, idioms)
- What NOT to do — common mistakes, footguns, files to leave alone
- Links to example apps or reference implementations
- Any environment setup an agent needs to run or test code

**What it should not contain:**
- Marketing or narrative — agents don't need persuasion
- Long prose introductions — start with the code
- Information that duplicates README content — link back instead

The README should always include a visible callout pointing agents to `AGENTS.md`,
ideally near the top of the Quickstart section where an agent starting a task will
encounter it early:

```markdown
Using an AI coding agent (Claude Code, Codex, Copilot)? Read [AGENTS.md](AGENTS.md) first.
```

---

## What not to do

- No ToC unless 6+ sections
- No em-dashes anywhere in the document. Not in headings, not in captions, not in file lists.
  Use full stops, semicolons, colons, or restructure.
- No `## Features` bullet list — the quickstart and usage show features better
- No `## License` section body — one-liner at the very bottom only: `MIT © 2025 Your Name`
  Exception: dual-license or commercial licensing models that genuinely require explanation.
- No `## Acknowledgements` unless it's a fork or has direct upstream deps to credit
- No `## Roadmap` unless explicitly requested
- No headers deeper than `###`
- No "A browser" as a prerequisite
- Never pad — a short README that gets someone running beats a long one they scroll past
- Never enumerate every flag or subcommand — point to `--help` instead
- Never recreate a real screenshot as SVG or Mermaid

---

## Generating the file

When asked to write a README:

1. Infer or ask for: project name, one-line description, Python version requirement,
   install method (pip/uv/pipx), whether a screenshot or demo exists, and hardware targets
2. If key info is missing, make reasonable assumptions and mark with `<!-- TODO: replace this -->`
3. Output as a fenced markdown block ready to copy, or write directly to `README.md`
   in an agentic/file context
4. After writing, note which `docs/` files should be created to support Customize
   and Read more

---

## Example skeleton

```markdown
# projectname

![build](https://img.shields.io/github/actions/workflow/status/org/projectname/ci.yml)
![pypi](https://img.shields.io/pypi/v/projectname)
![license](https://img.shields.io/pypi/l/projectname)
![python](https://img.shields.io/pypi/pyversions/projectname)

[Why paragraph: what problem this solves, what it replaces or skips]

[Who paragraph: who it's for, what SDK or stack it targets]

![architecture](docs/architecture.png)

## Install

### Prerequisites

- [Docker](https://docs.docker.com/compose/) with Compose v2
- Python >= 3.10
- Tested on Mac (Apple Silicon), Linux, and Raspberry Pi 4/5. Windows not yet tested.

### Setup

\`\`\`sh
git clone git@github.com:org/projectname.git
\`\`\`

\`\`\`sh
cd projectname
pip install -e .
\`\`\`

## Quickstart

### Start it

\`\`\`sh
projectname start
\`\`\`

[What just happened, what to look at next. 1-2 sentences.]

### Try it with an agent

The built-in demo path uses Ollama. Any MCP client works; Ollama is just the quickest
way to get something running.

\`\`\`sh
ollama pull model-name
\`\`\`

\`\`\`sh
ollama serve
projectname chat
\`\`\`

[What to try, what to expect, where to go next.]

Run \`projectname --help\` for the full command list.

Using an AI coding agent (Claude Code, Codex, Copilot)? Read [AGENTS.md](AGENTS.md) first.

## Connect your agent

Point any MCP client at \`http://localhost:8765/mcp/\`:

\`\`\`python
# minimal connection example
\`\`\`

[docs/integrating.md](docs/integrating.md) for full SDK samples.

## Customize

[Customize opener matching locked tone example]

- \`src/projectname/handlers/\` : one file per handler; filename becomes the key
- \`src/projectname/config.py\` : dataclass config; all fields documented inline
- \`pyproject.toml\` : optional extras \`.[dev]\`

[docs/customising.md](docs/customising.md)

## Read more

- [AGENTS.md](AGENTS.md) : orientation for AI coding agents
- [docs/architecture.md](docs/architecture.md) : what runs where, and why
- [docs/integrating.md](docs/integrating.md) : connect any MCP-speaking agent
- [CONTRIBUTING.md](CONTRIBUTING.md) : how to contribute
- [CHANGELOG.md](CHANGELOG.md)

---

MIT © 2025 Your Name
```