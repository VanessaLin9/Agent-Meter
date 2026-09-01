# Contract Index

這個目錄保存與程式碼一起版本化的實作契約。Notion 保存產品主計畫；此處只保存程式碼與 consumer 必須共同遵守、可由 review 或 tests 驗證的 invariant。

## Required reading

- [`usage-api.md`](usage-api.md)：normalized snapshot 與 HTTP semantics。
- [`provider-adapter.md`](provider-adapter.md)：adapter responsibility、result 與 failure taxonomy。
- [`security-and-testing.md`](security-and-testing.md)：credential boundary、redaction、fixtures 與 CI 規則。
- [`../../schemas/usage-v0.1.schema.json`](../../schemas/usage-v0.1.schema.json)：machine-readable response contract。
- [`../architecture/overview.md`](../architecture/overview.md)：component boundaries 與 dependency direction。

## Change policy

- Contract 變更必須由 task 明確授權。
- Breaking change 必須更新 `schema_version`，並說明 consumer migration。
- Schema、文件、fixtures 與 tests 必須在同一個 change 中保持一致。
- Implementation convenience 不是單獨改變 contract 的理由。
- Notion 與 contract 衝突時，不得自行選一邊；先回報衝突並確認決策。
