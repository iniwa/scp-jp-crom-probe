# SCP-JP Crom probe

Cromの公開GraphQL APIを用いて、指定日時以降に作成されたSCP-JPのJPオリジナル記事を取得するエンドツーエンド試験です。

## v2で修正した点

初版は `actions/setup-python` に `cache: pip` を指定しながら、リポジトリ内に `requirements.txt` / `pyproject.toml` がありませんでした。その場合、Pythonセットアップ段階で失敗し、その後 `if: always()` で動いたartifactアップロードが `probe-result.json` を見つけられず、元の原因を隠す二次エラーになることがあります。

v2では以下を修正しています。

- `cache: pip` を削除
- `requirements.txt` を追加
- Pythonセットアップ前に診断用の仮JSON・Markdown・ログを作成
- 出力先を `artifacts/scp-jp-crom-probe/` に固定
- ファイル単体ではなく出力ディレクトリ全体をartifact化
- 各ステップの成否を `workflow-diagnostics.txt` に記録
- Crom問い合わせ前に失敗しても、必ず診断artifactを残す

## 配置

ZIP内の**中身**をリポジトリのルートへ配置してください。

```text
<repository root>/
├─ .github/workflows/scp-jp-crom-probe.yml
├─ scripts/scp_jp_crom_probe.py
├─ requirements.txt
└─ README.md
```

`scp-jp-crom-probe/` フォルダごと既存リポジトリの下へ置くと、ワークフローから `scripts/...` と `requirements.txt` を見つけられません。

## GitHub Actionsでの実行

1. `Actions` → `SCP-JP Crom probe` → `Run workflow`
2. 初回は以下のまま実行

```text
2026-07-26T00:00:00+09:00
```

3. 実行後、Job summaryと `scp-jp-crom-probe` artifactを確認

artifactには必ず次が入ります。

- `probe-result.json`
- `probe-summary.md`
- `probe.log`
- `workflow-diagnostics.txt`

## 取得条件

- `http://scp-jp.wikidot.com` 配下
- 指定日時以降に作成
- `jp` タグあり
- 非表示ではない
- `fragment` / `deleted` カテゴリーを除外
- SCP-JP公式新着フィード相当の記事タグを持つ

また、次の4ページを個別照会します。

- SCP-4037-JP
- SCP-4543-JP
- SCP-4733-JP
- SCP-4119-JP

## 成功判定

- `Query Crom` が成功
- `probe-result.json` の `status` が `ok`
- 対象4ページの `indexed`、`in_date_range`、`present_in_recent_query` が確認できる

Crom通信やGraphQL処理に失敗した場合は `status: error` として記録し、新着0件とは扱いません。
