# SCP-JP Crom unified probe v4

既存の詳細プローブを、**JPオリジナル＋翻訳記事の統合取得試験**へ拡張したものです。

## 監視対象

- SCP報告書
- Tale
- GoIフォーマット
- アートワーク
- ハブ・サイト
- 合作・設定集
- エッセイ
- ニュース
- 外部ウィキアーカイブ

次は明示的に除外します。

- 著者ページ・作者ページ・訳者ページ
- コンポーネント
- テーマ
- `fragment` / `deleted`
- 非表示ページ

## 翻訳判定

単純な `jp` タグの有無だけではなく、次を組み合わせます。

1. Cromの `TRANSLATOR` attribution
2. クレジット欄の `翻訳責任者` / `翻訳者` / `翻訳年`
3. `原題`
4. `元記事リンク`
5. 原語支部タグ

判定結果は次の4種類です。

- `jp_original`
- `translation`
- `unknown`
- `conflict`

原語支部タグだけで翻訳と推定できた場合は `probable` とし、workflowを失敗扱いにします。`unknown`、`conflict`、本文取得失敗も同様です。取得失敗や分類不能を「新着なし」とは扱いません。

## 追加するファイル

現在の `iniwa/scp-jp-crom-probe` リポジトリへ、次を追加してください。

```text
.github/workflows/scp-jp-crom-unified-probe.yml
scripts/scp_jp_crom_unified_probe.py
tests/test_unified_parser.py
```

既存のv2/v3プローブは残して構いません。

## 実行

GitHub Actionsから次を手動実行します。

```text
SCP-JP Crom unified probe
```

初回入力値:

```text
2026-07-26T00:00:00+09:00
```

## 出力

Artifact `scp-jp-crom-unified-probe`:

```text
unified-result.json
unified-summary.md
probe.log
workflow-diagnostics.txt
articles/jp_original/*.source.txt
articles/jp_original/*.text.txt
articles/translation/*.source.txt
articles/translation/*.text.txt
articles/unknown/*
articles/conflict/*
```

`unified-summary.md`には、JPオリジナルと翻訳記事を別セクションで表示します。翻訳記事には原語支部、原題、元記事リンク、翻訳者、分類確度を出力します。

## 合格条件

- `Status: ok`
- 既存のJPオリジナル4件がすべてPresent
- `Unknown/conflicting: 0`
- すべての候補記事でContentが`yes`
- 翻訳記事が存在する場合、`Translations`表に掲載される
- Safety capによる打ち切りが`False`

翻訳記事が0件でも、それ自体では失敗にしません。対象期間に本当に投稿がなかった可能性と、抽出条件の問題を出力内容から確認します。
