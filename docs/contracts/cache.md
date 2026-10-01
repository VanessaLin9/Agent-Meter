# Disk Cache Contract

Machine-readable source: [`../../schemas/cache-v1.schema.json`](../../schemas/cache-v1.schema.json)

GET /usage 200 仍是 [`usage-api.md`](usage-api.md)。本檔只定義 checkout 外的 last-good 落地格式與重啟恢復。envelope metadata 不得出現在 public usage payload。

## Document

- `cache_version`：固定為 `1`。未知版本視為損壞 cache，忽略、不刪檔。
- `snapshot`：完整 schema-valid usage v0.1 snapshot，可含目前已停用的 provider last-good。
- 禁止 raw provider response、settings token、exception 或任意 extra 欄位。

檔案位於 `RuntimePaths.snapshot_cache_file`（`Library/Caches/Agent Meter/snapshots/snapshot.json`）。測試注入 temp dir。

## Load / save

- 缺檔是正常啟動，不是 fault。
- malformed／truncated／unknown version／oversized／permission／symlink：忽略該 cache、留下原檔、記錄 sanitized `cache` category。Health 為 `degraded` + `cache_error`。
- 寫入 atomic temp + replace，目錄 `0700`、檔案 `0600`。replace 前把 temp chmod 成 `0600`。失敗只清自己的 temp，不清空使用者目錄。
- save 失敗保留 memory last-good，health `degraded`；下一次成功寫入清掉 persistence fault。
- 單一 store owner 序列化 writer；reader 永遠得到完整舊版或完整新版，不得看到半份 JSON。

## Restart projection

1. 先讀成功儲存的 settings。
2. 讀 cache envelope（若有）。
3. 依 `enabled_providers` 投影，並用注入 clock 重算 freshness／aggregate。
4. `collected_at` 與 refresh-failed `stale` 保留。`generated_at` 更新不代表資料變 fresh。
5. 仍在 threshold 內的 `ok` 維持 `ok`；過期轉 `stale`；只有新的 success 能解除失敗造成的 stale。時鐘倒退維持 fresh。
6. 停用的 provider 不得出現在 public snapshot。active map 為空時回「無 snapshot」，不得呼叫 `build_snapshot({})`。

## Ownership

- Merge／freshness：`src/agent_meter/cache.py`、`freshness.py`、`aggregation.py`（無 I/O）
- Disk store／protocol：`src/agent_meter/cache_store.py`
- HTTP：尚未建立；只能拿到 projected `UsageSnapshot` 或既有 503 envelope
