# Credential Leak Incident Response

## Do not

- 不要在 issue、PR、chat、terminal transcript 或 screenshot 重貼 secret。
- 不要只刪檔案再 commit；舊 commit 仍包含資料。
- 不要先花時間重寫 history 才 rotate credential。
- 不要讓其他 branch／clone 繼續 push 污染 history。

## 1. Contain

- 立即停止 push、merge、release 與 deployment。
- 記錄受影響的 provider、credential 類型、檔案路徑與 commit SHA；只記 metadata，不記 secret value。
- 若內容曾進入 Agent output、log、commit、remote、PR、artifact 或無法確定暴露範圍，視為已洩漏。

## 2. Revoke or rotate first

- 在 provider 端 revoke／rotate credential。
- 若 provider 同時有 access token 與 refresh token，依 provider 指引處理整組 session。
- 驗證舊 credential 已無法使用。
- 每台部署主機重新 provision 新 credential，不透過 Git 傳遞。

## 3. Remove repository traces

- 尚未 commit：移除檔案／內容，確認不在 staged diff，再執行 Gitleaks working-tree scan。
- 已 commit、尚未 push：重寫本機 commit，掃描完整 local history；若 secret 曾出現在其他輸出，仍應 rotate。
- 已 push：使用 GitHub 建議的 `git-filter-repo` 流程清理所有 branches／tags／refs，並依需要聯絡 GitHub Support 清理 cached references。
- 清理 GitHub Actions artifact、release asset、cache、PR comment、log 與其他外部副本。
- 所有舊 clone 必須重新 clone 或依協調流程清理，避免 recontamination。

History rewrite 是清理，不是 credential remediation；rotate／revoke 才能終止使用風險。

## 4. Verify

- Gitleaks working tree scan 通過。
- Gitleaks full Git history scan 通過。
- GitHub Secret Scanning 沒有未處理 alert。
- 舊 credential 確認失效，新 credential 僅存在允許的 per-host secret boundary。
- Application、cache、API、logs 與 fixtures 不含 raw secret 或 upstream payload。

## 5. Prevent recurrence

- 新增或收緊 Gitleaks rule；allowlist 只限精確 placeholder。
- 補 regression test，確認 error/log redaction。
- 檢查 task 是否不必要地授權 Agent 讀取 live credential。
- 更新 `SECURITY.md`、`AGENTS.md` 或 provider contract。
- 記錄 root cause 與 control gap，但不記錄 secret value。

官方參考：[Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
