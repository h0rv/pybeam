---
name: documentation-writing
description: Write, edit, and review clear technical documentation using Google's technical writing guidance and PyBEAM's Python-first standards; use for README files, guides, API prose, docstrings, tutorials, and example explanations.
---

# Documentation writing

Help the reader finish a task. Explain only what they need to act correctly.
Use this skill before drafting or reviewing documentation, including small edits.

## Establish the reader and the task

1. Read the repository's [instructions](../../../AGENTS.md), the page, its linked
   examples, and the implementation behind any behavior you describe.
2. Identify what the reader wants to do and what they already know. For PyBEAM,
   assume ordinary async Python experience, not Erlang, Elixir, or BEAM knowledge.
3. Choose one purpose: teach a first task, solve a specific problem, explain a
   concept, or provide API reference. Do not turn a how-to into an architecture tour.
4. Put the useful action or answer first. Introduce concepts when the reader needs
   them, not in a glossary they must learn before running Python.

## Write for PyBEAM users

- README and `docs/guide/` describe the intended end-state experience for an
  average Python developer. Installation is `uv add python-beam`; imports use
  `pybeam`. Do not substitute the unrelated `pybeam` distribution name.
- Keep mise, native builds, platform matrices, wheel packaging, VM setup, and
  BEAM/OTP/Hornbeam implementation explanations off user pages. Put contributor
  setup in `docs/development.md` and design investigations in `docs/research/`.
  Use real public import paths in examples, even when a path names an integration.
- Present one normal path that works without infrastructure plumbing. Explain
  optional or advanced operations separately, only when they help the task.
- Keep semantics that affect correctness: state lifetime, lost state after restart,
  timeouts that do not undo work, cancellation, ownership, and overload. Explain
  these in Python terms beside the operation; do not hide them to sound simple.
- End-state presentation is not permission to invent APIs, output, benchmarks, or
  guarantees. Check claims against source and relevant research. If implementation
  blocks the intended experience, report that to the requester rather than adding
  temporary build workarounds to the guide or claiming an unrun check passed.

## Draft clear prose

- Describe only current behavior. Do not narrate history: no "used to", "no
  longer", "has since", "was fixed" or "before this change".
- Use active voice, present tense, and second person. Name who performs an action.
  Prefer direct verbs: "Run", "Save", "Return", or "Stop".
- Keep each sentence focused on one idea and each paragraph on one topic. Use a
  consistent name for each concept; resolve ambiguous "it", "this", and "they".
- Use sentence-case headings that name the task or concept. Number steps when
  order matters; use bullets for unordered items and tables for comparisons.
- Put conditions before actions and results after them. Give prerequisites only
  when the task needs them. State the filename, command, and expected result where
  the reader would otherwise have to guess.
- Use code formatting for identifiers and commands, descriptive link text, and
  language-tagged fenced code blocks. Avoid "click here", idioms, and unexplained
  acronyms. Give useful images alternative text; do not rely on color alone.
- Cut throat-clearing, repeated introductions, summaries that repeat the page,
  roadmap chatter, and marketing claims. Remove "simply", "just", "powerful",
  "seamless", "leverage", and "it's important to note" unless they add meaning.
  Do not replace precise limits with vague reassurance.

For example:

| Instead of | Write |
| --- | --- |
| In this section, we will explore how to leverage shared state. | Store values across requests. |
| The actor will be restarted by the supervisor. | The group restarts the failed actor. |
| Save this as `main.py` and run `python main.py`. It prints `Hello`. | Put `# main.py` first in the Python example, then show `$ uv run main.py` and `Hello` in a shell block. |
| State is managed automatically. | Values live in memory and are lost when the program stops. |

## Make examples usable

- Show the smallest complete example that teaches the operation. Include imports,
  entry point, and cleanup; omit unrelated frameworks and abstraction scaffolding.
- Follow `modern-python` and the repository's example conventions. Do not use
  pseudocode, ellipses, omitted-code comments, or invented output in runnable samples.
- Published guide Python fences must byte-match their associated files in
  `examples/docs/`, in the order listed in `site/src/links.mjs`. Edit the source and
  fence together. Do not repeat a generic installation or hello-world section on
  every concept page.
- Put the actual filename in a comment on the first line of each Python example,
  such as `# main.py`. Follow each runnable example with a `console` shell block
  showing `uv run <file>` and its exact output. Keep imported helper files beside
  the entry file; show the command for the entry file, not the helper.
  Never narrate "Save this as X and run Y. It prints Z."
- Verify claimed results with the appropriate checks. Site tests verify source
  matching and presentation, not native behavior; run relevant runtime checks when
  behavior changes and report anything you could not verify.

## Review before handing off

Read the rendered page as the intended Python developer, then make a deletion pass:

- Can the reader find the first action without learning runtime internals?
- Are commands, imports, links, and expected results correct and consistent?
- Are failure and state boundaries explicit wherever they change how code is used?
- Does each sentence add an action, explanation, decision, or necessary constraint?
- Are contributor setup and transient project status absent from user pages?

For repository documentation changes, run the existing site checks from the root:

```sh
cd site
npm test
DOCS_BASE=/ npm run build
DOCS_BASE=/pybeam/ npm run build
```

Only pages in `site/src/links.mjs` are published. Keep this skill and other agent or
contributor material out of that allowlist. For docstring-only changes, run the
relevant Python checks instead; for Python example edits, run both sets of checks.

## Writing references

These are the general baseline; repository instructions and the PyBEAM audience
rules above take precedence. Consult the relevant section for unfamiliar cases,
not the entire guide for every edit.

- [Google technical writing fundamentals](https://developers.google.com/tech-writing/one)
- [Google developer documentation style highlights](https://developers.google.com/style/highlights)
- [Writing procedures](https://developers.google.com/style/procedures)
- [Formatting and explaining code samples](https://developers.google.com/style/code-samples)
