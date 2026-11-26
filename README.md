# claude-skills

個人用の Claude Code プラグインのマーケットプレイス。

| プラグイン | 内容 |
|---|---|
| [blind-code-review](plugins/blind-code-review/skills/blind-code-review/SKILL.md) | PR説明・commit・コメント・テストを伏せてコードだけでレビューし、その評価を基準に文章側の主張を検証する2段階レビュー |

## インストール

Claude Code で次を実行する(private の間は、手元で `git clone` できる認証が必要)。

```
/plugin marketplace add riawiththesam/claude-skills
/plugin install blind-code-review@riawiththesam-skills
```

## ライセンス

[MIT](LICENSE)
