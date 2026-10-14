# セキュリティポリシー / Security policy

[日本語](#日本語) · [English](#english)

## 日本語

### 脆弱性の報告

セキュリティ上の問題は **公開の ISSUE に書かないでください**。
[GitHub の非公開の脆弱性報告](https://github.com/atsuki-sakai/cloud-ops-team/security/advisories/new) から非公開で報告してください。
数日以内に受け付けの連絡をします。修正はパッチバージョンとしてリリースし、ご希望がない限りアドバイザリーに報告者としてお名前を記載します。

### 対象範囲

対象となるもの:

- エージェントにクラウドのリソースを変更させる方法、または `OPS_DRY_RUN`、ISSUE の起票上限、自動クローズの設定を回避する方法
- 秘密情報、トークン、個人情報が ISSUE、コメント、レポート、ログ、トレースに漏れること（`app/core/redact.py` の漏れ）
- ログのメッセージや ISSUE・PR の内容を通じたプロンプトインジェクションで、意図しない書き込みが起きること
- デプロイスクリプトの認証の弱さ（たとえば誰でも呼び出せるサービスになってしまうこと）

対象外: 外部サービスの問題（その提供元に報告してください）と、エージェントが監視している環境そのもののセキュリティ状態です。
後者はエージェント自身が報告する内容です。

### 運用者向けの安全対策チェックリスト

- 監視対象のクラウドでは、エージェントに **読み取り専用** の権限だけを与える（`scripts/gcp/grant_target_roles.sh`）
- GitHub トークンは対象リポジトリに限定した **fine-grained トークン** を使う（Issues は書き込み、Pull requests・Actions・Contents・
  Metadata は読み取り）。トークンはシークレット管理サービスに保存する
- サービスは **非公開** にし（認証なしの呼び出しを許可しない）、定期実行専用のアカウントから呼び出す
- 最初は `OPS_DRY_RUN=true` で始め、必要がなければ `OPS_AUTO_CLOSE_ISSUES=false` のままにする
- エージェントが作成した ISSUE を自分の AI コーディングツールに渡すときは、信頼できない入力として扱う

## English

### Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
[GitHub private vulnerability reporting](https://github.com/atsuki-sakai/cloud-ops-team/security/advisories/new).
You will get an acknowledgement within a few days; fixes are released as patch versions and credited in the advisory unless you
prefer otherwise.

### Scope

In scope:

- Ways to make the agents change cloud resources or bypass `OPS_DRY_RUN`, the issue cap or the auto-close setting
- Leaks of secrets, tokens or personal data into issues, comments, reports, logs or traces (gaps in `app/core/redact.py`)
- Prompt injection through log messages, issue or PR content that leads to unintended writes
- Authentication weaknesses in the deployment scripts (for example a service that anyone can invoke)

Out of scope: findings in third-party services (report them to the vendor) and the security posture of the environments the
agents monitor — that is what the agents themselves report.

### Hardening checklist for operators

- Run the agents with **read-only** roles on the monitored cloud (`scripts/gcp/grant_target_roles.sh`)
- Use a **fine-grained GitHub token** limited to the target repository (Issues: write; Pull requests, Actions, Contents,
  Metadata: read) and store it in a secret manager
- Keep the service **private** (no unauthenticated invocation) and call it with a dedicated scheduler identity
- Start with `OPS_DRY_RUN=true` and keep `OPS_AUTO_CLOSE_ISSUES=false` unless you need it
- Treat issues created by the agents as untrusted input when your own AI coding tools act on them
