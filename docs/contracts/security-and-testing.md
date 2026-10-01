# Security and Testing Contract

## Trust boundaries

- Provider-owned login state 是 sensitive input，只能在 Local Collector 的 adapter boundary 使用。
- Normalized domain model、snapshot cache、HTTP API 與 ESP32 都是不含 credential 的區域。
- Batch 2 desktop API 只 bind `127.0.0.1`。不得 bind `0.0.0.0` 或 LAN。loopback 不是認證；它只降低遠端暴露。同源 UI 可以 `PUT /settings`，這是本批唯一使用者寫入能力，不得變成 wildcard CORS 或跨站可寫。
- LAN bind 與 device authentication 仍未決定；在那之前不得預設可安全暴露到 private LAN 或 public network。
- Settings 不含 provider token／session。Prefs、normalized cache 與 Claude mailbox 都在 checkout 外的使用者目錄。

## Never persist or emit

- Access token、refresh token、cookie、API key。
- `Authorization` 或 provider-specific authentication headers。
- Cursor machine ID、checksum 或完整 local storage dump。
- Account email、user ID 或 billing identifier，除非未來產品需求明確要求且完成 privacy review。
- 未清理的 raw upstream response、exception object 或 request dump。

## Logging

允許記錄：

- Provider ID、sanitized source ID。
- Start/end、duration、result category、retry count。
- Missing field name、validation rule、HTTP status class。
- 是否使用 last-good fallback，以及資料 age。

禁止記錄：

- Header/body 全文、token prefix、credential length。
- 真實 credential path、machine identifier、完整 account payload。
- 可能包含 secret 的 traceback locals 或 serialized exception context。

Error message 必須在 adapter boundary sanitized。若無法確定 upstream message 是否安全，改用穩定 error code 與自有描述。

## Cache and local files

- Cache 只能保存 schema-valid normalized snapshot。
- 寫入必須 atomic，不能讓 reader 看到 partial JSON。
- Cache file 不包含 raw response 或 credential。
- Settings 目錄 `0700`、settings 檔 `0600`；拒絕不安全 symlink target。損壞或未知版本不得默默重設成全啟用。
- Local runtime files 不進 Git；fixture 必須獨立人工建立。
- 若未來需要保存敏感設定，必須另行決定 storage、permission 與 rotation policy。v1 settings 禁止 secret 欄位。

## CI tests

- 必須 offline、deterministic、repeatable。
- 不得依賴本機已登入的 Cursor、Codex、Claude session。
- Mock network、subprocess、clock 與 filesystem boundary。
- 使用固定 clock 驗證 freshness、reset countdown 與 stale transition。
- Valid fixtures 必須通過 JSON Schema；invalid fixtures 必須因預期 rule 失敗。
- Failure-path coverage 與 happy-path 同等重要。

## Live smoke tests

- 必須由使用者明確執行，不放進預設 test command 或 CI。
- 執行前說明會讀取哪個 provider-owned state、是否發出 network request、是否可能 refresh token。
- 輸出只顯示 normalized/sanitized values。
- 不保存 raw response；若為 debug 臨時保存，需在 task 結束前安全移出 repository 並說明。
- Cross-check provider UI 時，只比較必要數值與 rounding tolerance。

## Fixtures

- 使用明顯虛構的 timestamps、percentages、IDs 與 error messages。
- 不從 live response 直接複製後只刪 token；應從契約重新手工建立最小 payload。
- 在 error sanitization test 中可植入明顯 fake secret，並斷言結果完全不包含該字串。
- Invalid usage fixtures 放在 `tests/fixtures/contracts/invalid/`，檔名描述預期違規。
- Settings／health／error fixtures 放在 `tests/fixtures/settings/` 與 `tests/fixtures/service/`，不得放進 `tests/fixtures/contracts/*.json`，以免被 usage snapshot smoke tests 誤讀。

## Review checklist

- 是否新增任何 credential read path？是否為唯讀且最小權限？
- 是否有 raw payload、headers 或 locals 進入 log？
- 是否所有 external calls 都有 timeout 與 cancellation behavior？
- CI 是否可能誤打 live provider？
- Fixture 是否可能來自真實帳號？
- Failure 是否保留 last-good data，且不顯示 false zero？
