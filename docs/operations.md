# 運用メモ

## 日次フロー

```text
12:20 JST GitHub Actions開始
  1. monitor-state読込
  2. Crom取得・JSON生成
  3. GitHub Pages公開
  4. 公開成功後にmonitor-state更新

12:40 JST ChatGPT Scheduled Task
  health.json → delta.json → 未通知IDのみ紹介
```

状態はPages公開後に保存します。これにより、Pages公開が失敗したのに記事だけが既読化されることはありません。

## 手動実行

通常の手動実行は次の設定です。

```text
window_days: 30
now: 空欄
force_bootstrap: false
```

通知候補は初回検出から72時間残るため、通常設定で再実行しても即座には消えません。

`force_bootstrap`は`monitor-state`を無視して`config/baseline.json`から再計算する診断機能です。繰り返し使用すると翻訳記事などが再び通知候補になるため、初期構築または障害調査時だけ使用してください。

## 障害時

### Crom取得・フィード生成の失敗

Artifact `scp-jp-monitor-output`の次を確認します。

```text
monitor.log
monitor-debug.json
monitor-summary.md
workflow-diagnostics.txt
artifact-manifest.txt
```

Pagesは更新されず、前回の正常データが残ります。ChatGPT側は`health.json`の日付が本日ではないことを検出し、取得失敗として通知します。

### Pagesデプロイ失敗

`monitor-state`は更新されません。次回実行で同じ記事が再び候補になります。ChatGPT側の`notification_id`記憶が二重通知を抑止します。

### 状態保存失敗

Pagesには新しいデータが公開済みですが、`monitor-state`は古いままです。次回実行で同じ候補が残る可能性があります。Workflowの`contents: write`権限、リポジトリのWorkflow permissions、ブランチ保護を確認してください。

## 状態ブランチ

`monitor-state`はActionsが自動管理します。`state.json`には次だけを保存します。

- `wikidot_id`
- 初回検出時刻・最終確認時刻
- ベースライン判定
- 直近通知候補用の短い記事スナップショット

古い記事のスナップショットは14日後に削除し、重複防止用のIDは保持します。

## 完全リセット

`monitor-state`ブランチを削除すると、次回はベースラインからbootstrapします。翻訳記事が再通知候補になるため、意図的な初期化時だけ実施してください。
