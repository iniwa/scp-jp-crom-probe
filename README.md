# SCP-JP Crom probe

Cromの公開GraphQL APIを用いて、指定日時以降に作成されたSCP-JPのJPオリジナル記事を取得するエンドツーエンド試験です。

## この試験で確認すること

- `http://scp-jp.wikidot.com` 配下のページを取得できるか
- 作成日時を基準に絞り込めるか
- `jp` タグ、非表示状態、`fragment` / `deleted` 除外が機能するか
- SCP-JP公式の新着フィードに相当する記事タグだけを抽出できるか
- 次の4ページがCromに登録され、日付範囲へ正しく分類されるか
  - SCP-4037-JP
  - SCP-4543-JP
  - SCP-4733-JP
  - SCP-4119-JP

## GitHub Actionsでの実行

1. このフォルダの内容を新規または専用のGitHubリポジトリへ配置します。
2. `Actions` → `SCP-JP Crom probe` → `Run workflow` を開きます。
3. 初回試験では `since_jst` を次のまま実行します。

```text
2026-07-26T00:00:00+09:00
```

4. 実行後、ジョブのSummaryを確認します。
5. `scp-jp-crom-probe` artifactから以下を取得できます。
   - `probe-result.json`: 機械判定用
   - `probe-summary.md`: 人間向け結果

## 成功判定

期待する基本結果は次のとおりです。

- ワークフローが成功する
- `status` が `ok`
- SCP-4037-JP、SCP-4733-JP、SCP-4119-JPが `indexed: true`
- 上記3件が7月26日以降なら `in_date_range: true`
- SCP-4543-JPは `indexed: true` だが、作成日が7月20日なら `in_date_range: false`
- 日付範囲内かつ公式新着記事相当のページは `pages` に列挙される

## 誤判定を防ぐ仕様

- API通信やGraphQL処理に失敗した場合、`新着なし`にはせず `status: error` で終了します。
- 日付は入力値からUTCへ変換します。`2026-07-26T00:00:00+09:00` は `2026-07-25T15:00:00+00:00` です。
- 最大500件で安全停止し、到達した場合は `truncated: true` になります。
- 新着候補の本文は取得しません。まずメタデータ取得の信頼性だけを検証するためです。

## ローカル実行

Python 3.14以上が必要です。

```bash
python -m pip install "thaumiel==0.1.0"
python scripts/scp_jp_crom_probe.py \
  --since-jst "2026-07-26T00:00:00+09:00"
```

## 日次監視へ進める場合

この試験が成功した後、次の段階で以下を追加します。

- JST基準の過去72時間を毎日重複取得
- URL単位の報告済み状態管理
- 新規記事だけ本文・ソース・別タイトルを追加取得
- 「SCP番号またはジャンル／記事タイトル・サブタイトル／ネタバレなし概要」の生成
- 主API失敗時に「新着なし」と誤判定しないエラー処理
