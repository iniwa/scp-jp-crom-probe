# SCP-JP Daily Monitor v5

Crom GraphQL APIからSCP財団日本支部の新着記事を取得し、毎日12:20頃（JST）にGitHub Pagesへ監視用JSONを公開する本番版です。

## 監視対象

### 含める

- SCP報告書
- Tale
- GoIフォーマット
- アートワーク・音楽
- ハブ・サイト
- 合作・設定集
- エッセイ
- ニュース
- 外部ウィキアーカイブ
- 上記に該当する翻訳記事

### 除外する

- 著者ページ・作者ページ・訳者ページ
- コンポーネント
- テーマ
- `fragment` / `deleted`
- 非表示ページ
- 上記の作品タグを持たない管理・技術ページ

## 構成

```text
.github/workflows/scp-jp-monitor.yml
config/baseline.json
scripts/scp_jp_monitor.py
tests/test_monitor.py
docs/scheduled-task-prompt.md
docs/operations.md
```

既存のv2～v4.2プローブは残して構いません。v5のワークフローは`scp_jp_monitor.py`だけを本番処理に使用します。

## 動作

```text
毎日12:20頃 JST
GitHub Actions
  ├─ monitor-stateブランチから前回状態を取得
  ├─ Cromから新着候補を取得
  ├─ JPオリジナル／翻訳を分類
  ├─ タイトル、サブタイトル、著者・翻訳者を正規化
  ├─ ネタバレなし概要作成用の短いsummary_basisを抽出
  ├─ GitHub Pagesへhealth.json / delta.json / latest.jsonを公開
  └─ Pagesデプロイ成功後にmonitor-stateブランチへ状態を保存

毎日12:40頃 JST
ChatGPT Scheduled Task
  ├─ health.jsonの鮮度と正常性を確認
  ├─ delta.jsonの未報告notification_idだけを抽出
  └─ 新着がある場合だけ通知
```

状態保存は**Pagesへのデプロイ成功後**に行います。デプロイ後の状態保存だけが失敗した場合、翌日に重複候補が出る可能性はありますが、記事を黙って取りこぼすことはありません。

## 初期ベースライン

`config/baseline.json`には、すでに紹介済みのJPオリジナル9件を登録しています。

翻訳記事はベースラインに含めていません。そのため、初回の本番実行では2026年7月26日以降の翻訳記事が`delta.json`に入り、一度まとめて紹介できます。

## 導入

ZIPの中身を`iniwa/scp-jp-crom-probe`のリポジトリルートへ配置し、コミット・プッシュします。

```text
<repository root>/
├─ .github/workflows/scp-jp-monitor.yml
├─ config/baseline.json
├─ scripts/scp_jp_monitor.py
├─ tests/test_monitor.py
└─ docs/scheduled-task-prompt.md
docs/operations.md
```

### GitHub Pagesを有効化

リポジトリで次を設定します。

```text
Settings
→ Pages
→ Build and deployment
→ Source: GitHub Actions
```

追加のAPIキーやSecretsは不要です。ワークフロー内の`GITHUB_TOKEN`でPagesデプロイと`monitor-state`ブランチ更新を行います。

リポジトリまたは組織のポリシーで`GITHUB_TOKEN`の書き込みが禁止されている場合は、ActionsのWorkflow permissionsで書き込みを許可してください。

## 初回テスト

GitHubのActionsから以下を実行します。

```text
SCP-JP daily monitor
→ Run workflow
→ window_days: 30
→ now: 空欄
```

初回の期待値は、おおむね次のとおりです。

```text
Status: ok
Mode: bootstrap
JP originals: 9以上
Translations: 16以上
New this run: 翻訳16件＋テスト時点までに追加された未報告記事
Pending: 0
```

実行成功後、以下が公開されます。

```text
https://iniwa.github.io/scp-jp-crom-probe/health.json
https://iniwa.github.io/scp-jp-crom-probe/delta.json
https://iniwa.github.io/scp-jp-crom-probe/latest.json
```

また、`monitor-state`ブランチが自動作成され、ルートに`state.json`が保存されます。

## 公開ファイル

### `health.json`

当日の取得状態です。

- `status: ok` — 全候補を正常に処理
- `status: degraded` — 一部記事が同期待ち・分類待ち。確定済み記事は公開
- `status: error` — 全体処理失敗。新しいPagesデプロイは行わず、前回成功版を維持

### `delta.json`

通知候補です。初回検出から72時間保持します。

主な項目:

- `notification_id` — `wikidot_id`由来の安定識別子
- `is_new_this_run`
- `edition` — `jp_original` / `translation`
- `genre`
- `article_title` / `subtitle`
- `summary_basis`
- `content_warnings`
- 翻訳記事の`source_branch`、`original_title`、`translators`

ChatGPT側は、過去に通知済みの`notification_id`を再通知しません。

### `latest.json`

直近30日間の確定済み記事一覧です。監査・取りこぼし確認に使用します。

## 障害時の扱い

- Crom全体への接続失敗、JSON異常、上限到達など: Workflow失敗。Pagesは前回成功版を維持
- 個別記事の本文が未同期: `degraded`。その記事は未検出扱いにせず`pending_pages`へ記録し、翌日再試行
- 分類不能・競合: `degraded`。対象ページは状態へ保存せず再試行
- 新着なし: `ok`かつ未報告候補0件。ChatGPTは通知しない
- `health.json`の日付が当日でない: ChatGPTは「本日の更新未完了」として障害通知

## 状態のリセット

通常は`monitor-state`ブランチを手動編集しません。

完全に初期化する場合は`monitor-state`ブランチを削除すると、次回実行が`config/baseline.json`からbootstrapします。この操作では翻訳記事が再び通知候補になるため、意図的なリセット時だけ行ってください。

## ローカル検証

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q scripts/scp_jp_monitor.py
```

ライブ取得には外部ネットワークが必要です。
