# Agent Meter Security Policy

Agent Meter 採 **credential-free repository**。即使 GitHub repository 是 private，也必須假設所有 committed content 最終可能被公開、複製或留在其他 clone。

## Core policy

- Provider credential 永遠不進 Git working tree、Git index、commit、fixture、cache、log 或 API response。
- `.env.example` 只保存非敏感設定與空 placeholder；repo-local `.env` 不得保存 provider token。
- 優先使用 provider-owned session：Codex app-server、Claude status-line input、Cursor local session。
- 部署到其他主機時，每台主機各自登入／provision credential；Git 只傳遞程式碼與非敏感 contract。
- Development／CI 預設完全 credential-free、offline、fixture-driven。
- Live access 必須由使用者明確啟用，且只限需要的 provider boundary。

## Runtime profiles

### Development and CI

- `AGENT_METER_MODE=development`
- `AGENT_METER_LIVE_MODE=0`
- 不讀 provider-owned credential state。
- 不發出 live provider request。
- 所有 network、process、filesystem 與 clock boundary 使用 mock／fixture。
- CI 不配置 Cursor、Codex 或 Claude 個人 credential。

### Local live smoke test

- 必須使用明確 `--live` 或等價 opt-in；不能由 default command 隱式觸發。
- 執行前說明將讀取哪個 provider-owned state、是否發 network request、是否可能修改登入狀態。
- 每次只允許 task 涉及的 provider。
- Adapter 只接收必要 credential；subprocess environment 使用 allowlist，不繼承完整 parent environment。
- stdout 只輸出 normalized data；diagnostics 送 stderr，兩者都必須 sanitized。
- 不保存 raw request／response、headers、token prefix、credential length 或 account identifier。

### Deployment host

- 每台主機獨立 provision，不從開發機複製 token 檔。
- macOS 優先使用 provider-owned session／Keychain reference。
- Linux／NAS 若必須使用額外 secret，放在 checkout 外，由 dedicated service user 讀取；目錄建議 `0700`、檔案建議 `0600`。
- 支援 secret reference（例如 OS secret store key 或 `/run/secrets/...`），避免 raw secret environment variable。
- Collector 只以執行所需的最小 OS 權限運作。

## Configuration boundary

Repo 可保存：

- `.env.example`。
- Default port、refresh interval、enabled provider IDs。
- Secret backend 名稱或 checkout 外的 config directory pointer。
- 假資料與 sanitized fixtures。

Repo 不得保存：

- Access／refresh token、cookie、API key、private key。
- Authorization header、Cursor machine ID／checksum。
- Provider local database、credential export、browser profile、HAR 或 raw response。
- 含真實 account email、user ID、billing ID 的 debug artifact。

## Defense in depth

- `.gitignore` 阻擋常見 secret／runtime file，但不視為安全邊界。
- Gitleaks pre-commit 掃描 staged content。
- GitHub Actions 掃描 PR、push 與完整 Git history。
- GitHub repository 建立後，必須啟用 Secret Scanning 與 Push Protection（方案允許時）。
- Scanner allowlist 只能針對精確 placeholder，不得排除整個 `docs/`、`tests/` 或 provider directory。
- Agent／reviewer 在 commit 前都要檢查 staged diff，不能只依賴 scanner。

本 repo pin Gitleaks CLI／pre-commit 到 `v8.30.1`，GitHub Action pin 到 `v3.0.0` 對應 commit SHA。升級必須獨立 review release notes 與 config compatibility。

## Agent restrictions

除非使用者明確授權 live security investigation，Agent 不得：

- 執行 `env`、`printenv` 或輸出完整 process environment。
- 搜尋、讀取、顯示或複製 credential value。
- 將 provider-owned login state 搬進 checkout。
- 使用真實 response 建立 fixture。
- 在 exception／traceback 中附帶 request headers、body 或 locals。
- 使用 `--no-verify`、`SKIP=gitleaks` 或 broad allowlist 繞過 secret scan。
- 修改 credential boundary、secret backend 或 token refresh policy 而沒有專屬 task。

## Repository setup checklist

- [ ] 安裝 Gitleaks `v8.30.1`。
- [ ] 安裝 pre-commit，執行 `pre-commit install`。
- [ ] 在第一次 push 前執行 `scripts/security-check.sh`。
- [ ] GitHub Secret Scanning 已啟用。
- [ ] GitHub Push Protection 已啟用。
- [ ] Default branch protection 已啟用，secret-scan check 為 required。
- [ ] 沒有 GitHub Actions provider credential。
- [ ] 每台 deployment host 各自完成 provider login／secret provisioning。

## Reporting and response

發現疑似洩漏時不要在 issue、PR 或 chat 貼出 secret。立即停止 push，依 [`docs/security/incident-response.md`](docs/security/incident-response.md) 執行 rotate／revoke 與清理。

GitHub 官方參考：

- [Push protection](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/prevent-future-leaks/enable-push-protection)
- [Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
- [GitHub Actions secrets](https://docs.github.com/en/actions/concepts/security/secrets)
