# Agent Meter

桌上型、always-on 的 AI agent usage dashboard。Local Collector 負責取得與統一 Cursor、Codex、Claude Code 的 quota；ESP32 + LCD 只負責讀取與顯示。

## Project plan

[Notion：Agent Usage Dashboard 實體顯示器前期研究與 Agent Meter v0.1 主計畫](https://app.notion.com/p/3cedf30cc71c81fba766c94890eb2ec1)

Notion 是專案目標、scope、milestones 與設計決策的唯一來源。實作 Agent 應唯讀參考；需要調整主計畫時，應回到 Notion 更新，不在 implementation PR 中修改專案方向。

Repository 只保存程式碼、測試、執行文件，以及實作所需的 API/schema contract。

## Development contracts

- [Agent 開發規則](AGENTS.md)
- [Security policy](SECURITY.md)
- [Contract index](docs/contracts/README.md)
- [Architecture overview](docs/architecture/overview.md)
- [Usage JSON Schema](schemas/usage-v0.1.schema.json)

開發與 CI 預設完全 credential-free。Provider token 不放在 repo-local `.env`；每台執行主機各自使用 provider-owned session 或 checkout 外的 secret storage。
