# Agent Meter — Agent Development Contract

本檔適用於整個 repository。所有 implementation agent 在修改程式碼前都必須先讀完本檔、`SECURITY.md`，以及任務涉及的 `docs/contracts/` 文件。

## Authority and scope

1. 使用者目前的明確指示與 task / PR acceptance criteria。
2. 本 repository 的 machine-readable schema、`SECURITY.md` 與 `docs/contracts/`。
3. Notion 的 Agent Meter 主計畫與設計決策。
4. 其他說明文件與程式碼註解。

主計畫位於 [Notion](https://app.notion.com/p/3cedf30cc71c81fba766c94890eb2ec1)。Notion 是產品目標、scope、milestones 與設計決策的唯一來源；implementation agent 不得在一般實作 PR 中自行改變這些內容。

契約或 credential boundary 變更必須由 task 明確授權，並在同一變更中更新 schema、文件、fixtures 與 tests。若 task 與既有契約衝突，先停止擴張 scope，提出衝突與影響，不得默默改變行為。

## Before coding

- 先讀 `README.md`、本檔、`SECURITY.md` 與相關 `docs/contracts/`。
- 使用 `rg` 找出相關 contract marker、provider boundary、tests 與既有命名。
- 確認本次 in scope、non-goals、failure policy 與 acceptance criteria。
- 先新增或更新 offline deterministic tests，再實作行為。
- 外部 provider 一律視為不可靠：timeout、缺欄位、malformed data、expired auth 與 partial failure 都是正常分支。
- Development／CI 預設不讀 credential、不發 live provider request。

## Architecture boundaries

- Provider adapter 只處理該 provider 的 discovery、authentication boundary、transport、upstream parsing 與 normalization input。
- Domain models 與 schema validation 不依賴 provider SDK 或 raw response shape。
- Orchestrator 保持薄：只負責排程、呼叫 adapters、套用 fallback、聚合狀態與寫 cache。
- API layer 只輸出 normalized snapshot，不暴露 raw provider payload。
- ESP32 是 dumb client，不包含 provider login、credential 或 provider-specific transport。
- 單一 provider 失敗不得阻斷其他 provider。
- Secret access 必須侷限於 adapter request-construction boundary；subprocess 使用 allowlisted environment，不繼承整個 parent environment。

## Agent-first documentation

程式碼必須讓下一個 agent 能快速定位用途、資料流與風險，但不要為明顯語法添加噪音註解。

每個重要 module 應在 module docstring 或檔案頂部說明：

- 此 module 的責任與 non-goals。
- 主要輸入、輸出及其單位。
- 所屬 contract 或 provider boundary。
- 由誰負責 retry、cache 與 fallback。

在下列位置使用固定、可搜尋的註解標記：

- `CONTRACT:` 跨 module 的資料或行為 invariant。
- `SECURITY:` credential、redaction、local file 或 network trust boundary。
- `PROVIDER:` upstream provider-specific assumption、欄位映射或版本風險。
- `FALLBACK:` failure 時保留、降級或中止的原因。

範例：

```python
# CONTRACT: reset_at is always UTC Unix seconds; Cursor upstream uses milliseconds.
# SECURITY: Only this request builder may receive the provider credential.
# PROVIDER: Cursor may omit spend fields even when percentage fields are present.
# FALLBACK: Keep the last valid snapshot and mark it stale after a refresh failure.
```

註解應解釋「為什麼、單位、邊界與失敗政策」，不要逐行翻譯程式碼。可以用以下命令快速盤點關鍵位置：

```bash
rg -n 'CONTRACT:|SECURITY:|PROVIDER:|FALLBACK:'
```

新增 provider 時，必須同步新增 `docs/providers/<provider>.md`，內容至少包含資料來源、auth boundary、欄位與單位、known missing fields、failure behavior、manual verification 與 upstream schema risk。

重大且長期有效的架構決策應新增 ADR；短期 task 狀態與 milestone 進度留在 Notion／PR，不複製到 repository。

## Credential and security rules

- Provider credential 永遠不進 Git working tree、index、commit、fixture、cache、log 或 API response。
- Repo-local `.env` 不得保存 provider token；`.env.example` 只能包含非敏感值與空 placeholder。
- 優先使用 provider-owned session；其他主機必須各自登入／provision，不能透過 Git 複製 token。
- Adapter 只能唯讀使用既有 login state，除非 task 明確授權修改或 refresh flow。
- ESP32 與 `GET /usage` response 永遠不得接觸 provider credential。
- Fixture 必須人工建立或完整 sanitized；不能直接 commit live response。
- 不得 log raw credential、完整 upstream response、request headers/body 或含敏感 locals 的 exception。

除非使用者明確授權 live security investigation，Agent 不得：

- 執行 `env`、`printenv` 或輸出完整 process environment。
- 搜尋、讀取、顯示、複製或量測 credential value／prefix／length。
- 將 provider-owned login state 搬進 checkout。
- 使用真實 response 建立 fixture。
- 以 `--no-verify`、`SKIP=gitleaks` 或 broad allowlist 繞過 secret scan。
- 修改 secret backend、credential boundary 或 token refresh policy。

完整規則與 incident response 見 `SECURITY.md`。

## Testing and machine-readable output

- CI tests 必須 offline、deterministic、repeatable；mock process、filesystem、network、credential 與 clock boundary。
- Live provider checks 只能是明確標示、由使用者批准的 manual smoke tests，不能在 CI 執行。
- 每個 external integration 都要測 happy path、timeout、empty output、malformed output、missing fields、auth failure 與 partial failure。
- stdout 若供另一個 process 解析，必須保持純 machine-readable；prompt、progress、warning、diagnostic 全部送 stderr。
- 修改 normalized response 時，必須同步更新 JSON Schema、fixtures、consumer tests 與相關 contract 文件。
- Error/log redaction tests 應植入明顯 fake secret，並斷言所有輸出都不包含該字串。

## Commit and review gates

- Commit 前執行 formatter、lint、type check、tests 與 `scripts/security-check.sh`。
- 不得跳過 Gitleaks hook；false positive 必須使用精確 allowlist 並說明原因。
- Credential boundary、scanner config、CI security workflow 與 allowlist 變更必須保持小而獨立，方便 security review。
- Reviewer 必須檢查 staged diff、new files、generated artifacts 與 secret-scan result，不只看 unit tests。

## Definition of done

- 行為符合 task acceptance criteria、`SECURITY.md` 與 repository contracts。
- 新增與修改的邏輯有對應的 offline tests，包含 failure paths。
- Schema 與 example fixtures 驗證通過。
- 文件與關鍵註解足以讓下一個 agent 找到責任邊界與重要 invariant。
- 沒有 unrelated changes、credential、machine-local data 或 raw upstream payload。
- Gitleaks working-tree scan 通過；Git 初始化後也必須通過 full-history scan。
- Formatter、lint、type check 與 tests 全部通過；若尚未建立某項工具，PR 必須清楚註明。
