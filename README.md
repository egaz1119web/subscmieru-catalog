# subscmieru-catalog

Android アプリ **サブスクみえーる** (`com.egaz.subscmieru`) が参照する料金カタログの配信元。

アプリのリリースなしに価格改定を反映するため、マスターデータだけをこのリポジトリに分離し
GitHub Pages で静的配信している。アプリ本体のソースは別リポジトリ (private)。

## 配信URL

| ファイル | URL |
| --- | --- |
| サービスカタログ | https://egaz1119web.github.io/subscmieru-catalog/v1/subscription_services.json |
| 変更履歴 | https://egaz1119web.github.io/subscmieru-catalog/v1/catalog_changes.json |

アプリは1日1回 `If-None-Match` (ETag) 付きで取得し、更新が無ければ `304` で終わる。
取得に失敗した場合・JSONが壊れていた場合は、アプリ同梱のスナップショットに自動フォールバックする。

## パスの `v1` について

`v1` は **スキーマ世代**。既存フィールドの削除・意味変更など後方互換性を壊す変更をしたくなったら
`v2/` を新設し、`v1/` は旧バージョンのアプリ向けにそのまま残す。
`v1/` を破壊的に変更すると、アップデートしていない利用者のアプリが壊れる。

各JSONの `schemaVersion` はアプリ側の対応世代チェックに使う。
アプリは自分が知っている世代より大きい `schemaVersion` のデータを無視して同梱版を使うため、
`v1/` の中で互換性のある拡張（フィールド追加など）をする分には値を変えなくてよい。

## 更新手順

1. `v1/subscription_services.json` の価格を修正する
2. 修正内容を `v1/catalog_changes.json` の `changes` に追記する（アプリの「変更のお知らせ」に出る）
3. **両ファイルの `version` を同じ値にインクリメントし、`updatedAt` を当日の日付にする**
4. `python3 tools/validate.py` が通ることを確認する
5. commit & push（数分でPagesに反映される）

`version` を上げ忘れると、アプリ側の「未読の変更あり」バッジが立たない。

## スキーマ

### subscription_services.json

```jsonc
{
  "schemaVersion": 1,
  "version": 6,                    // 更新のたびにインクリメント
  "updatedAt": "2026-08-29",       // ISO-8601 (YYYY-MM-DD)
  "services": [
    {
      "id": "netflix",             // 一意。変更すると登録済みユーザーとの紐付けが切れる
      "name": "Netflix",
      "category": "VIDEO",         // ServiceCategory の enum ID
      "domain": "netflix.com",     // ロゴ取得用。不明なら省略
      "packageName": "com.netflix.mediaclient",  // インストール済みアプリ検出用。任意
      "plans": [
        {
          "name": "広告つきスタンダード",
          "price": 890,            // JPYなら円、USDならセント
          "cycle": "MONTHLY",      // MONTHLY | YEARLY
          "currency": "USD"        // 省略時は JPY
        }
      ]
    }
  ]
}
```

### catalog_changes.json

```jsonc
{
  "schemaVersion": 1,
  "version": 6,                    // subscription_services.json と揃える
  "updatedAt": "2026-08-29",
  "changes": [
    {
      "serviceId": "elevenlabs",
      "serviceName": "ElevenLabs",
      "category": "AI",
      "domain": "elevenlabs.io",
      "kind": "PRICE_UP",          // 未知の kind は読み飛ばされる
      "plan": "Starter",
      "old": 500,
      "new": 600,
      "oldCurrency": "USD",
      "currency": "USD",
      "oldCycle": "MONTHLY",
      "cycle": "MONTHLY"
    }
  ]
}
```

## 注意

- 登録済みユーザーの金額は登録時のスナップショットとして端末内に保持されるため、
  ここを更新しても既存の登録データが勝手に書き換わることはない。
- `id` は永続的な識別子。リネームは避ける（どうしても必要なら `kind: RENAME` で履歴に残す）。
- 価格は各社公式サイトの公開情報に基づく概算。誤りを見つけたら Issue か PR で歓迎。
