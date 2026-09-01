# Contract Fixtures

- `ok.json`：三個 provider 都有 fresh data。
- `partial.json`：同時包含 healthy、stale 與 unavailable provider。
- `error.json`：沒有任何可顯示 provider data。
- `invalid/`：必須被 schema validator 拒絕的 payload。

所有資料都是手工建立的虛構值，不得替換成 live provider response。

Milestone 0 選定 Collector stack 後，必須把 Draft 2020-12 JSON Schema validator 加入預設 test command 與 CI，並斷言：

- `ok.json`、`partial.json`、`error.json` 驗證成功。
- `invalid/*.json` 驗證失敗，而且 failure 指向檔名描述的 contract rule。

目前沒有為此預先引入 Python 或 Node dependency，避免在 stack 尚未確認前鎖定工具鏈。
