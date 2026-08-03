# 運用メモ

## 日次フロー

```text
06:17 JST GitHub Actions予定時刻
  1. monitor-state読込
  2. Crom取得・JSON生成
  3. GitHub Pages公開
  4. 公開成功後にmonitor-state更新

12:40 JST ChatGPT Scheduled Task
  health.json → delta.json → 未通知IDのみ紹介
```

GitHub Actionsの定刻実行には遅延があり得るため、Scheduled Taskまで約6時間の余裕を確保しています。状態はPages公開後に保存します。これにより、Pages公開が失敗したのに記事だけが既読化されることはありません。

## 手動実行

通常の手動実行は次の設定です。

```text
window_days: 30
now: 空欄
force_bootstrap: false
```

通知候補は初回検出から168時間（7日間）残るため、Scheduled Taskが数日停止しても復旧後に拾い直せます。

増分取得の開始時刻は`max(現在時刻-30日, bootstrap_since_jst)`です。監視開始日時より前へルックバックしないため、古い記事が後日新着として混入しません。

`force_bootstrap`は`monitor-state`を無視して`config/baseline.json`から再計算する診断・復旧機能です。繰り返し使用すると翻訳記事などが再び通知候補になるため、初期構築または明示的な復旧時だけ使用してください。

## v5.1.1移行時の一度限りの復旧

v5.0運用中に通知候補が72時間で失効し、30日ルックバックから監視開始日前の記事が誤登録された可能性があるため、v5.1.1を`main`へ反映した後に一度だけ次の設定で手動実行します。

```text
window_days: 30
now: 空欄
force_bootstrap: true
```

実行後は次を確認します。

1. WorkflowとPagesデプロイが成功している。
2. `health.json.status`が`ok`または`degraded`である。
3. `health.json.query.notification_retention_hours`が`168`である。
4. `delta.json.retention_hours`が`168`である。
5. `health.json.query.since_jst`が`2026-07-26T00:00:00+09:00`以降である。
6. 続けて`force_bootstrap: false`で再実行し、監視開始日前の記事が`new_this_run`へ追加されないことを確認する。
7. ChatGPT Scheduled Taskの本文を`docs/scheduled-task-prompt.md`の内容へ置き換える。

ChatGPT側は過去に通知済みの`notification_id`を記憶しているため、bootstrapで候補が再生成されても通知済み記事は再通知せず、取りこぼしていた記事だけを通知します。

2回目の通常実行で`new_this_run`が増える場合は、実際にその間にCromへ新規反映された記事かを`delta.json.new_this_run_ids`で確認してください。監視開始日前の記事が含まれる場合は運用を止めてください。

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

Pagesは更新されず、前回の正常データが残ります。ChatGPT側は`health.json.generated_at_jst`が現在時刻から36時間を超えて古くなった時点で、取得失敗として通知します。`generated_date_jst`が今日と異なるだけでは失敗扱いにしません。

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
