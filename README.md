# SCP-JP Crom detail probe v3

メタデータ取得に成功したCromプローブの次段階です。指定日時以降の対象記事について、Cromから次を個別取得します。

- ページソース
- レンダリング済み本文
- Cromの概要フィールド
- クレジット情報
- 別タイトル
- 著者情報

ページソース内のクレジット欄から `タイトル:` を抽出し、SCP報告書では番号とサブタイトルを分離します。例えば、

```text
SCP-4733-JP - 吝嗇の飲食
```

を次のように出力します。

```text
記事タイトル: SCP-4733-JP
サブタイトル: 吝嗇の飲食
```

## 追加するファイル

現在の `iniwa/scp-jp-crom-probe` リポジトリへ、ZIP内の次の3か所を追加してください。

```text
.github/workflows/scp-jp-crom-detail-probe.yml
scripts/scp_jp_crom_detail_probe.py
tests/test_detail_parser.py
```

既存のv2プローブは残して構いません。

## 実行

GitHubのActionsから次を実行します。

```text
SCP-JP Crom detail probe
```

入力値は、初回はそのままです。

```text
2026-07-26T00:00:00+09:00
```

## 出力

Artifact `scp-jp-crom-detail-probe` に次を保存します。

```text
detail-result.json
detail-summary.md
workflow-diagnostics.txt
articles/<ページ名>.source.txt
articles/<ページ名>.text.txt
```

`detail-summary.md`には記事タイトル、サブタイトル、JSTの投稿日時、本文プレビューが入ります。本文全体は `articles/` に保存します。

## 合格条件

- Statusが `ok`
- Eligible articlesとDetails fetchedが一致
- SCP-4037-JP、SCP-4543-JP、SCP-4733-JP、SCP-4119-JPがすべてPresent
- 各記事で `Content` が `yes`

本文取得や期待記事の確認に失敗した場合、結果ファイルを残したうえでworkflowを失敗扱いにします。取得失敗を「記事なし」とは判定しません。
