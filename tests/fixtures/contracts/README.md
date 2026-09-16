# Contract Fixtures

- `ok.json`：三個 provider 都有 fresh quota data。
- `ok-with-spend.json`：Cursor 兩個 monthly quota pool 加上 optional on-demand spend meter。
- `ok-zero-and-full.json`：quota remaining 0 與 100，以及 `reset_at: null`。
- `partial.json`：同時包含 healthy、stale 與 unavailable provider。
- `error.json`：沒有任何可顯示 provider data。
- `invalid/`：必須被 JSON Schema 與 typed model 同時拒絕的 payload。

所有資料都是手工建立的虛構值，不得替換成 live provider response。
