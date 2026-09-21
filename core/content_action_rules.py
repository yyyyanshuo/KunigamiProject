"""Localized prompt contract for model-driven document editing actions."""


RULES = {
    "zh": """## 人设、关系与计划维护指令
只有在确实需要改变已有资料时才使用；每个标签独占一行。old/content/定位内容必须逐字复制当前资料，不得概括或模糊匹配；找不到时系统不会修改。

人设：
- `[ADD_PERSONA: {"content":"追加到人设末尾的内容"}]`
- `[DELETE_PERSONA: {"old":"要删除的第一处原文"}]`
- `[EDIT_PERSONA: {"old":"第一处原文","new":"新表达"}]`
- `[REWRITE_PERSONA: {"content":"完整新人设"}]`
LOCK 区块只允许读取；增删改不得触碰，重写必须原样保留全部 LOCK 区块与标签。

关系：增删改只操作指定 target 的 description。
- `[ADD_RELATION: {"target":"对象原名","content":"追加描述"}]`
- `[DELETE_RELATION: {"target":"对象原名","old":"第一处原文"}]`
- `[EDIT_RELATION: {"target":"对象原名","old":"第一处原文","new":"新表达"}]`
- 完整重写可按 target、role、score、description 顺序使用 `[REWRITE_RELATION: ["对象原名","身份",5,"完整描述"]]`。
- 部分或乱序重写必须用命名参数，如 `[REWRITE_RELATION: {"target":"对象原名","description":"完整新描述","score":5}]`；未提供字段保持原样。

计划：date 存在表示有时间计划；不提供 date 表示无时间计划。定位必须同时匹配原日期状态和完整内容。
- `[ADD_PLAN: {"date":"2026-08-20","content":"计划"}]`；无时间时省略 date。
- `[DELETE_PLAN: {"date":"2026-08-20","content":"完整原计划"}]`；无时间时省略 date。
- `[EDIT_PLAN: {"from":{"date":"2026-08-20","content":"完整原计划"},"set":{"date":"2026-08-21","content":"新计划"}}]`。from 省略 date 表示从无时间计划寻找；set 省略字段表示保持原值，set.date 为 null 表示移到无时间计划。
- `[REWRITE_PLAN: {"at":{"date":"2026-08-20","content":"完整原计划"},"content":"完整新内容"}]`。at 省略 date 表示无时间计划；重写不改变时间。""",
    "ja": """## ペルソナ・関係・予定の編集アクション
実際に資料を変更する必要がある時だけ使い、各タグを独立した行に出力する。old/content/検索内容は現在の資料から一字一句そのままコピーし、曖昧検索や要約をしない。
- Persona: `[ADD_PERSONA: {"content":"追記"}]`, `[DELETE_PERSONA: {"old":"最初の原文"}]`, `[EDIT_PERSONA: {"old":"最初の原文","new":"新表現"}]`, `[REWRITE_PERSONA: {"content":"完全な新ペルソナ"}]`。LOCK 内は変更禁止で、全面書換えでも全 LOCK を完全保持する。
- Relation: ADD/DELETE/EDIT は target の description のみを操作する。`[ADD_RELATION: {"target":"対象名","content":"追記"}]`, `[DELETE_RELATION: {"target":"対象名","old":"原文"}]`, `[EDIT_RELATION: {"target":"対象名","old":"原文","new":"新表現"}]`。全面指定は `[REWRITE_RELATION: ["対象名","役割",5,"完全な説明"]]`、部分指定は `[REWRITE_RELATION: {"target":"対象名","description":"新しい完全説明","score":5}]` を使い、省略フィールドは保持する。
- Plan: date があれば日付付き、なければ日付未定。`[ADD_PLAN: {"date":"2026-08-20","content":"予定"}]`, `[DELETE_PLAN: {"date":"2026-08-20","content":"完全な元予定"}]`, `[EDIT_PLAN: {"from":{"date":"2026-08-20","content":"完全な元予定"},"set":{"date":"2026-08-21","content":"新予定"}}]`, `[REWRITE_PLAN: {"at":{"date":"2026-08-20","content":"完全な元予定"},"content":"完全な新内容"}]`。未定予定では locator の date を省略し、set.date=null は未定へ移動、REWRITE_PLAN は日付を変えない。""",
    "en": """## Persona, relationship, and plan editing actions
Use these only when stored material truly needs to change, one tag per line. Copy every old/content locator verbatim from the current material; matching is exact and only the first match is changed.
- Persona: `[ADD_PERSONA: {"content":"append"}]`, `[DELETE_PERSONA: {"old":"first exact text"}]`, `[EDIT_PERSONA: {"old":"first exact text","new":"replacement"}]`, `[REWRITE_PERSONA: {"content":"complete new persona"}]`. Never edit LOCK blocks; a rewrite must preserve every LOCK block verbatim.
- Relationship ADD/DELETE/EDIT affect only target.description: `[ADD_RELATION: {"target":"exact target name","content":"append"}]`, `[DELETE_RELATION: {"target":"exact target name","old":"first exact text"}]`, `[EDIT_RELATION: {"target":"exact target name","old":"first exact text","new":"replacement"}]`. Full positional rewrite: `[REWRITE_RELATION: ["target","role",5,"complete description"]]`. Partial/out-of-order rewrite must use named fields: `[REWRITE_RELATION: {"target":"target","description":"complete new description","score":5}]`; omitted fields stay unchanged.
- Plan: a present date means dated; omitted date means undated. Use `[ADD_PLAN: {"date":"2026-08-20","content":"plan"}]`, `[DELETE_PLAN: {"date":"2026-08-20","content":"complete old plan"}]`, `[EDIT_PLAN: {"from":{"date":"2026-08-20","content":"complete old plan"},"set":{"date":"2026-08-21","content":"new plan"}}]`, or `[REWRITE_PLAN: {"at":{"date":"2026-08-20","content":"complete old plan"},"content":"complete new content"}]`. Omit locator dates for undated plans; omit set fields to preserve them; set.date=null moves to undated; REWRITE_PLAN never changes time.""",
}


def get_content_action_rules(lang="zh"):
    return RULES.get(lang, RULES["zh"])
