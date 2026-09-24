# Provider Documentation

每個 v0.1 provider 在開始實作 adapter 時，都要新增一份 `<provider>.md`。這裡記錄 upstream integration contract，不記錄真實 credential 或 live response。Claude Code parser、Codex app-server adapter 與 Cursor period-usage parser 已落地，見 `claude.md`、`codex.md` 與 `cursor.md`。Cursor Connect RPC transport 尚未建立。

## Required template

```markdown
# <Provider> Adapter

## Purpose and non-goals

## Data source and stability

## Authentication boundary

## Upstream fields and units

## Normalized meter mapping

## Missing or optional fields

## Timeout and failure behavior

## Credential and logging rules

## Offline fixtures

## Manual smoke-test procedure

## Known risks and upgrade signals
```

文件要能回答：資料從哪裡來、哪些欄位不穩定、timestamp／percentage 單位、失敗後誰保存 last-good snapshot，以及下一個 agent 如何在不暴露 credential 的情況下驗證行為。
