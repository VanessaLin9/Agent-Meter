# Provider Adapter Contract

## Responsibility

Adapter 將單一 provider 的 machine-local session 與 upstream response 轉成 validated provider data。Adapter 不負責 HTTP API、global status aggregation、persistent cache 或 ESP32 rendering。

## Conceptual interface

實際語言可以不同，但行為需等價：

```text
collect(context, deadline) -> ProviderCollectionSuccess | ProviderCollectionFailure
```

Success 必須包含：

- 穩定 `provider_id` 與 sanitized `source`。
- `collected_at` UTC Unix seconds。
- 一個以上 validated meters。
- 可選的非敏感 metadata；不能直接保存 raw response。

Failure 必須包含：

- 穩定 error `category`。
- Sanitized、可供 debug 的 `message`。
- `retryable` boolean。
- 可選的 provider error code，但不得含 token、request header 或 account identifier。

## Failure taxonomy

- `not_configured`
- `not_installed`
- `not_authenticated`
- `auth_expired`
- `timeout`
- `network`
- `upstream`
- `malformed_response`
- `internal`

Adapter failure 不直接決定 `stale` 或 `error`。Orchestrator 根據是否存在 last-good snapshot 套用：

- 有 last-good data → `stale`。
- 無 last-good data，且 provider 不可使用 → `unavailable`。
- 無 last-good data，且執行失敗 → `error`。

## Required behavior

- 所有 filesystem、process 與 network 操作都接受 bounded deadline。
- 驗證 raw shape、required fields、range 與 timestamp unit 後才建立 domain model。
- Optional field 缺失不能讓 required meter 無條件失敗。
- 不支援或 ambiguous 的欄位應省略，不猜測、不偽造。
- Adapter 不能自行更新 shared cache；只回傳 typed result。
- Retry 由 orchestrator 控制，避免 adapter 與 orchestrator 疊加造成 retry storm。
- Process 必須在 timeout/cancellation 後清理，不留下 orphan process。

## Authentication boundary

- 優先重用 provider client 已管理的登入狀態或 authenticated local interface。
- 只讀取完成 request 所需的最少資料。
- 不把 credential 複製到 project directory、fixture、cache 或 API model。
- Token refresh 若需要修改 provider-owned state，必須有 task 明確授權並在 provider 文件記錄。
- Credential path 與 account identifier 不得出現在一般 log；必要 debug 僅輸出 sanitized location category。

## Process and stdout contract

若 adapter 與 subprocess 透過 stdout 傳 JSON：

- stdout 只能包含 machine-readable protocol data。
- Prompt、progress、warning 與 diagnostics 全部送 stderr。
- Consumer 必須分別處理 non-zero exit、timeout、EOF、empty stdout、malformed JSON 與混雜輸出。
- JSON-RPC response 必須依 `id` 配對，不能假設第一行 response 就是目標結果。

## Provider-specific documentation

每個 adapter 必須有 `docs/providers/<provider>.md`，並記錄：

- Upstream interface 與穩定性（official / local structured / unofficial）。
- Auth source 與 read/write boundary。
- Raw fields、units、optional fields 與 normalized mapping。
- Timeout、failure category 與 manual smoke-test 方法。
- Upstream schema change 的偵測信號。

## Required tests

- Healthy response mapping。
- 每個 required field 缺失。
- Optional field 缺失。
- Percentage boundary：0、100、負數、超過 100。
- Seconds / milliseconds conversion。
- Timeout、network failure、auth failure、malformed response。
- Empty/mixed stdout 與 subprocess non-zero exit（若適用）。
- Sanitized error 不包含 fixture 中植入的 fake secret。
