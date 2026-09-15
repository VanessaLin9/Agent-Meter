# Agent Meter

桌上型、always-on 的 AI agent usage dashboard。Local Collector 負責取得與統一 Cursor、Codex、Claude Code 的 quota；ESP32 + LCD 只負責讀取與顯示。

## Project plan

[Notion：Agent Usage Dashboard 實體顯示器前期研究與 Agent Meter v0.1 主計畫](https://app.notion.com/p/3cedf30cc71c81fba766c94890eb2ec1)

Notion 是專案目標、scope、milestones 與設計決策的唯一來源。實作 Agent 應唯讀參考；需要調整主計畫時，應回到 Notion 更新，不在 implementation PR 中修改專案方向。

Repository 只保存程式碼、測試、執行文件，以及實作所需的 API/schema contract。

## Collector development

Milestone 0 目前只有 package 骨架與品質檢查，還沒有 provider adapter、HTTP API 或 cache。

Required local toolchain:

- Python 3.12.14（見 `.python-version`）
- uv 0.12.13

Install:

```bash
uv sync --locked --group dev
```

Re-lock dependencies only when `pyproject.toml` changes, then review `uv.lock`.

Quality checks (format check, lint, mypy, pytest; does not rewrite the lockfile or auto-fix format):

```bash
bash scripts/quality.sh
```

Manual format fix:

```bash
uv run --locked ruff format src tests
```

Secret scan remains `bash scripts/security-check.sh`. CI runs the same `scripts/quality.sh` on macOS and Ubuntu. After the first green runs exist, a repo admin should add the actual GitHub check-run names—expected `Quality (macOS)` and `Quality (Linux)`—as required checks on `main`, and keep Gitleaks. That admin step is not part of this foundation change.

## Development contracts

- [Agent 開發規則](AGENTS.md)
- [Security policy](SECURITY.md)
- [Contract index](docs/contracts/README.md)
- [Architecture overview](docs/architecture/overview.md)
- [Architecture decision records](docs/adr/README.md)
- [Usage JSON Schema](schemas/usage-v0.1.schema.json)

開發與 CI 預設完全 credential-free。Provider token 不放在 repo-local `.env`；每台執行主機各自使用 provider-owned session 或 checkout 外的 secret storage。
