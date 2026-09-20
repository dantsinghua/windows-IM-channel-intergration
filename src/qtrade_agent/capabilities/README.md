# 能力目录(02 §3.10)

- 每个能力一份 JSON Schema(draft 2020-12),文件名 = `<op>.json`;运行期部署到 `/opt/qtrade/agent/capabilities/`。
- `kind ∈ {read, write, admin}`、`danger` 十项定死(02 §3.10 表;本目录本期只放 M2 骨架用到的三个非 danger 能力)。
- 指代目标会话/联系人的参数**一律命名 `session`**(02 §2.2.2);CI 规则:带会话语义的属性名不是 `session` 即构建失败(tests/test_capabilities.py)。
- `send_text.text` 的内容校验(R6-48 `clean_text(text)==text`)不在 schema 里表达,由 `bus/validate.py` 做。
