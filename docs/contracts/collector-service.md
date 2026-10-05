# Collector Service Lifecycle Contract

常駐 Collector 的 start/stop、排程、generation fencing 與單實例鎖。HTTP mapping 仍在 [`desktop-service.md`](desktop-service.md)。Cache merge 仍在 [`cache.md`](cache.md)。

## Ownership

| Concern | Owner |
| --- | --- |
| Lifecycle / settings apply / generation | `src/agent_meter/collector_service.py` |
| Poll cadence numbers | `src/agent_meter/service_policy.py` |
| Lazy live adapters | `src/agent_meter/provider_registry.py` |
| Instance lock | `src/agent_meter/instance_lock.py` |
| Wall vs monotonic clocks | `src/agent_meter/clocks.py` |
| Merge / freshness | `cache.py`、`freshness.py`、`aggregation.py` |
| Disk last-good | `cache_store.py` |
| FastAPI | 尚未建立（B2-04） |

Scheduler 只接線。它不得複製 second merge／fallback，也不得在 GET 路徑觸發 provider I/O。

## Clocks

- Wall clock：`collected_at`、`generated_at`、freshness。單位 UTC Unix seconds。
- Monotonic clock：interval、backoff、job duration。不得拿去當資料年齡。
- 兩者都可注入。Production 用系統時鐘；測試用可控 fake clock，禁止靠 `time.sleep` 推進 cadence。

## Enablement and generation

每個 provider 有獨立 generation。disable 與 re-enable 都遞增。每次 poll job 帶上 dispatch 當下的 generation。

- 晚到的 success／failure、取消後才回來的結果、先關再開前那一筆，都不得寫 memory／disk 或改 status。
- 已送出的 request 無法撤回。文件不承諾「沒有任何 in-flight I/O」。
- 同 provider 同時最多一個 in-flight job（含舊 generation）。off→on 等舊 job 結束後立刻 refresh，不接續舊 generation，也不另佔一條 worker。
- 重新啟用可立刻投影 last-good，但 `collected_at` 不刷新；沒有 last-good 時在第一次 typed 結果前是 `snapshot_not_ready`。

## Polling

- Codex／Cursor：啟用後立刻收一次，之後每 300 秒。timeout 20 秒傳給 adapter `deadline_seconds`。
- 同 provider 同時最多一個 in-flight job（含舊 generation）。off→on 等舊 job 結束後立刻 refresh，不接續舊 generation，也不另佔一條 worker。
- 錯過的 tick 不堆積，slot 空了最多補一次。
- 成功：下一輪以 start+300 為準（過期則立刻一次）。
- 失敗：不在同一 job 內重試。下次等待 300 → 600 → 900 秒後封頂。re-enable 重置失敗計數。
- 單一 collect worker 不得串行擋住其他 provider。並行上限是 polled provider 數量（每 provider 一條 daemon thread），禁止無限制 thread。
- cache persist 合到獨立 daemon writer；collect slot 在 fsync 前釋放。卡住的 persist 不得讓另一個 provider 停止 refresh。
- Claude 不走 poller。

## Claude event source

本張只提供介面。啟用但沒有 event、也沒有 last-good → `not_configured`／`unavailable`，訊息說明等待 status-line。不得呼叫 Claude API。mailbox bridge 留給後續任務。`ingest_claude_event` 必須帶當下 generation。

## Settings apply

序列化。順序：驗證 → atomic persist → commit runtime generation／active set。成功回傳代表新設定已生效。persist 失敗或 CAS conflict：runtime 不動。未 `start()` 或已 `stop()` 時拒絕 apply，不寫 disk。`start()` 從 disk 重建 `_next_due`，不依賴 stop 前的 in-memory `_enabled`。cache persist 不得佔 `_apply_lock`，也不得佔 collect slot。

關閉的 provider 立刻離開 public projection，不再排新工作。GET／health／usage 只讀投影並重算 age。cache persist 不得佔共用 state lock。

## Instance lock and shutdown

- 同一 `RuntimePaths.config_dir` 只能有一個 service。第二個 instance 拒絕啟動；錯誤不含 path 或 secret。
- lock file：`config_dir/service.lock`。以 `O_NOFOLLOW` 開啟，拒絕 symlink。start 可建立缺的 private config dir，但不得把既有過寬目錄 chmod 成 `0700` 來通過 settings 檢查。
- SIGINT／SIGTERM：停新排程、bump generation、等待 in-flight collect 與 persist writer（上限 `POLL_TIMEOUT_SECONDS + 5`）、請求最後一次 cache flush。persist 已結束才釋放鎖；若 persist 仍卡住，鎖留到同一 lifecycle epoch 的 writer idle，或 process 退出關掉 lock fd。restart 的 acquire／lifecycle bump 與 writer 放鎖必須在同一把 persist condition 上互斥。
- cache persist 必須在持有 `_apply_lock`（settings／Claude ingest／start）或仍佔 collect in-flight（poll finish）時 enqueue，fsync 本身仍在獨立 daemon writer。不得在釋放 shutdown fencing 之後才排隊寫入。
- Collect 與 persist worker 必須是 daemon thread。`ThreadPoolExecutor` 的 non-daemon worker 會在 interpreter shutdown 被 join，卡住的 adapter／fsync 會讓 `python -m agent_meter.service` 在 `stop()` 返回後仍不退出。
- Adapter 必須遵守 `deadline_seconds`。Python thread 殺不掉；shutdown 後晚到結果仍被 generation 丟掉。

## Logging

只記 `provider`、`result`、`duration_ms`、`category`、`retry`。禁止序列化 exception、traceback 或 adapter 原文。

## Fallback

| 情況 | 行為 |
| --- | --- |
| `enabled_providers=[]` | health `idle`；usage `no_enabled_providers`；零 live I/O |
| 已啟用、active set 未齊 | `snapshot_not_ready` |
| 第一次 typed failure | schema-valid `error`／`unavailable`，可形成完整 snapshot |
| 設定損壞 | health `degraded`+`config_error`；不排程 |
| cache save 失敗 | memory last-good 留下；health `cache_error` |
| 未 start／已 stop 的 apply | `ServiceNotRunningError`；不寫 disk |
| 晚到舊 generation | 丟棄 |
| 第二 instance | `ServiceAlreadyRunningError` |
