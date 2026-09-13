---
name: deep-research
description: Multi-source web research that fans out over subagents and answers with citations.
argument-hint: "<question or topic>"
allowed-tools: [web_search, web_extract, delegate_task, read_file, glob, grep, memory_write, memory_search]
---

# Deep research

Answer a question about the world outside this repository, from sources you
actually read, with a citation on every claim. The question is:

**$ARGUMENTS**

A research answer without URLs is a guess with formatting. If you cannot find a
source for a claim, say the claim is unsourced or drop it.

## Language

Answer in the language the user asked in, decided from the question itself, and
stay there for the whole report. Write natively; do not translate the English in
this file, and never mix scripts inside one sentence. Technical terms that are
normally left in English (library names, protocol names, metrics such as p95)
stay in English inside the sentence. Quotes from sources keep their original
language, followed by a one-line gloss when the report's language differs.

## Method

### 1. Check what is already known

Call `memory_search` on the question's key terms first. A previous session may
have answered part of this, and repeating the search costs the user time and
tokens for nothing. If the repository is the subject of the question, read the
relevant files before you search the web: local facts beat web facts about local
code, always.

### 2. Cut the question into facets

Write two to five **independent** facets — independent meaning a different search
would answer each one. Show them to the user before searching:

```
Facets
  F1 <what this facet establishes>
  F2 <what this facet establishes>
```

Good facets separate concerns that a single search would blur: the official
specification, the actual implementations, the known failure modes, the
alternatives, what changed recently.

### 3. Fan out

Delegate one subagent per facet with `delegate_task`, all in the same turn so
they run in parallel. Give each one a self-contained brief — it cannot see this
conversation:

```
delegate_task(
  task="Research <facet>. Use web_search to find sources and web_extract to read
        the promising ones. Prefer primary sources: official documentation,
        specifications, release notes, the project's own repository. Treat blog
        posts and forum answers as evidence about practice, not about the spec,
        and say which you are citing. Answer with 3-6 findings, each one sentence
        followed by its URL. Say explicitly when the sources disagree or when you
        found nothing.",
  tools=["web_search", "web_extract"]
)
```

Five subagents is the practical ceiling. Past that the searches start returning
the same pages.

### 4. Read the disagreements

When two facets contradict each other, that is the finding. Do not average them
and do not quietly pick one. Say what each source claims, note which is primary
and which is dated, and if the contradiction matters to the user's decision, run
one more targeted search to settle it.

Check dates on everything. A confident answer from three years ago about a fast
moving library is worse than no answer.

### 5. Answer

```markdown
## <the question>

**Short answer.** <two or three sentences, the thing the user came for>

### Findings
1. <claim> — [<source title>](<url>)
2. <claim> — [<source title>](<url>)

### Where sources disagree
<what each says, which is primary, what you concluded and why — omit when they don't>

### Confidence
<high | medium | low>, because <what would change the answer>

### Sources
- [<title>](<url>) — <what it is: spec, docs, blog, issue thread>
```

Then call `memory_write` once with the short answer and the two or three most
load-bearing URLs, tagged `research`, so the next session inherits the result
instead of the search.

## Rules

- Every claim in Findings carries a URL. No exceptions, including claims you are
  confident about.
- Say "I could not find a source for this" rather than filling the gap.
- Quote sparingly and attribute; never present extracted text as your own prose.
- Note when a source is the vendor of the thing being evaluated.
- If the question has no web component at all, say so and answer from the
  repository instead of searching for the sake of it.

## Differences from OMC

`oh-my-claudecode` 4.15.1 ships no `deep-research` skill. This one composes its
method from two OMC skills (both MIT): `skills/external-context/SKILL.md` for
facet decomposition and parallel fan-out, and `skills/autoresearch/SKILL.md` for
the evidence discipline and the durable record. The differences from those
originals:

- **One shot, not a mission loop.** OMC's `autoresearch` owns a single mission
  across many runs, keeps `.omc/autoresearch/<slug>/` with per-iteration
  evaluation JSON and a decision log, and stops on a max-runtime ceiling. This
  skill answers one question in one turn; what persists is a single
  `memory_write`, which is snowpea's replacement for that artifact tree.
- **No evaluator contract.** `autoresearch` requires a structured evaluator
  emitting `{pass, score}` and iterates through failures. Research questions have
  no such predicate, so the stopping rule here is facet coverage plus an explicit
  confidence statement.
- **Generic subagents, not `document-specialist`.** OMC fans out over its
  `document-specialist` agent via the `Task` tool at a pinned model. snowpea has
  no agent-type catalogue; `delegate_task` with a `tools` restriction gets the
  same isolation, and the model is whatever the session is configured for.
- **`web_search` / `web_extract` instead of `WebSearch` / `WebFetch`.** snowpea's
  tools are provider-neutral and free-first, so results come from whichever
  search backend `search.provider` names.
- **The disagreement section is mandatory when it applies.** OMC's
  `external-context` synthesises findings into one list. Collapsing a real
  contradiction into a single confident bullet is the most common way research
  output misleads, so it gets its own heading here.
- **No cron integration.** OMC documents scheduling `autoresearch` through Claude
  Code's native cron. In snowpea a repeating research run is an ordinary job:
  `/schedule` it, and each run appends its own memory record.
