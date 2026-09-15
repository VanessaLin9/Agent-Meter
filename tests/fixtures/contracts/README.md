# Contract Fixtures

- `ok.json`：三個 provider 都有 fresh data。
- `partial.json`：同時包含 healthy、stale 與 unavailable provider。
- `error.json`：沒有任何可顯示 provider data。
- `invalid/`：必須被 schema validator 拒絕的 payload。

所有資料都是手工建立的虛構值，不得替換成 live provider response。

Milestone 0 只對 `ok.json`、`partial.json`、`error.json` 做 stdlib JSON 與頂層欄位 smoke，不是 JSON Schema validation。完整 Draft 2020-12 validator 與 `invalid/*.json` 對應測試留給 models / schema task。
