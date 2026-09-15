# ADR 0001: Collector foundation uses Python 3.12 and uv quality tooling

- Status: Accepted for Milestone 0 dispatch
- Date: 2026-09-13
- Task: [Collector foundation 與 quality tooling](https://app.notion.com/p/3cfdf30cc71c81e69222c6047ea88a40)
- PR: [#1](https://github.com/VanessaLin9/Agent-Meter/pull/1)

## Context

Agent Meter's Collector must spawn provider processes, read stdin JSON, and later read local SQLite session state. Milestone 0 only needs a reproducible install and quality gate. Domain models, HTTP API, cache, and provider adapters are later tasks.

TypeScript/Node can implement the same Collector. Node has built-in `node:sqlite`, so SQLite is not a reason to exclude it.

## Decision

Use Python 3.12 with uv, Ruff, mypy, and pytest for the Collector foundation.

- Runtime dependencies stay empty in this milestone.
- Pydantic waits for the models task.
- FastAPI waits for the HTTP API task.
- Local macOS and Ubuntu CI are in scope; Linux provider login, Pi/NAS, and Windows are not.

## Consequences

The repository now has an independent CPython interpreter and static-check toolchain. uv lockfiles and mypy strict mode are the compensating controls. Changing language later would replace this foundation rather than layering a second application runtime.
