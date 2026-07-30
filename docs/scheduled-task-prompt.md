# ChatGPT Scheduled Task用プロンプト

## 実行設定

- 実行時刻: 毎日12:40頃
- タイムゾーン: Asia/Tokyo
- タスク名: SCP-JP新着記事確認

## タスク本文

毎日、SCP財団日本支部の新着記事を確認してください。データ源は以下のGitHub Pages上のJSONだけを使用し、Wikidotの新着一覧やRSSを主データ源にしないでください。

```text
https://iniwa.github.io/scp-jp-crom-probe/health.json
https://iniwa.github.io/scp-jp-crom-probe/delta.json
https://iniwa.github.io/scp-jp-crom-probe/latest.json
```

次の手順を厳守してください。

1. `health.json`を取得し、`generated_date_jst`が今日の日付（Asia/Tokyo）であることを確認する。
2. `health.status`を確認する。
   - `ok`: 通常処理を続ける。
   - `degraded`: 確定済みの記事は処理を続けるが、末尾に取得待ち・分類待ちの記事があることを簡潔に記載する。
   - `error`、JSON取得失敗、JSON構文不正、`generated_date_jst`が今日ではない、`query.truncated`が`true`: 新着なしとは判断せず、「本日のSCP-JP新着確認に失敗した」と障害内容だけを通知する。
3. `delta.json`を取得する。
4. `delta.json.articles`のうち、以下をすべて満たす記事だけを通知対象とする。
   - `baseline`が`false`。
   - このタスクの過去の実行で同じ`notification_id`を通知していない。
   - `edition`が`jp_original`または`translation`。
5. 過去に通知した`notification_id`を、このタスク内で重複防止用に記憶する。同じIDは、タイトル・URL・サブタイトルなどが変化しても再通知しない。
6. 新着が0件で、`health.status`が`ok`なら、ユーザー向けの通知を何も送らない。「新着なし」「更新はありませんでした」なども送らない。
7. 新着がある場合だけ、投稿日時の新しい順に、JPオリジナルと翻訳記事を分けて通知する。存在しないセクションは表示しない。
8. 内容の説明には各記事の`summary_basis`だけを主な根拠として使用する。結末、後半の補遺、重大な正体、どんでん返しを明かさず、1～3文のネタバレなし概要にする。本文から確認できない内容を推測しない。
9. `content_warnings`が空でなければ、内容警告として簡潔に表示する。
10. 記事リンクには`url`を使用する。

通知形式は次のとおりです。

```markdown
# SCP-JP 新着記事

## JPオリジナル

### SCP-XXXX-JP — サブタイトル

- **記事ジャンル:** SCP報告書
- **投稿日:** 2026年7月31日 10:25 JST
- **著者:** XXXX
- **概要:** ネタバレなしの簡単な紹介。
- **内容警告:** 必要な場合のみ記載
- **記事:** 記事URL

## 翻訳記事

### SCP-XXXX — 日本語サブタイトル

- **記事ジャンル:** 翻訳SCP報告書
- **原語支部:** EN
- **原題:** Original title
- **原著者:** XXXX
- **翻訳者:** XXXX
- **日本支部投稿日:** 2026年7月31日 10:20 JST
- **概要:** 日本語訳本文に基づくネタバレなしの簡単な紹介。
- **内容警告:** 必要な場合のみ記載
- **記事:** 日本語版URL
- **原文:** 元記事URLがある場合のみ記載
```

記事のフィールドが欠けている場合は、存在する情報だけを使い、推測で補完しないでください。
