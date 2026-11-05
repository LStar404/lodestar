"""块类型注册表（9.5.3 扩展点）：类型 → 图标/中文名/导出模板/生成约束，全项目查表驱动。

域 profile（5.8）经本注册表注入激活类型集：math（教材五类）、paper（论文八类）、
physics/cs/medicine/chemistry 等学科预设；用户可用 config 的 generate.block_types
定义新类型（或覆盖预设的 hint/图标），由 install_types 注入后全链路生效：
生成 schema、提示词类型指南、校验器、前端图标同一事实源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import SchemaViolation


@dataclass(frozen=True)
class BlockTypeSpec:
    type_id: str
    icon: str
    cn_name: str
    tex_env: str | None          # tex 导出环境名；None → 降级为无编号段落
    template: str                # 导出模板片段名（src/export/templates/）
    source_policy: str           # required | optional | none
    allowed_relations: tuple[str, ...] = field(default_factory=tuple)
    synthesis_allowed: bool = False
    profiles: tuple[str, ...] = ("textbook", "paper")
    hint: str = ""               # 生成提示词里的写作指引（自定义类型的核心字段）


SOURCE_RELATIONS = ("defines", "states", "proves", "motivates", "illustrates", "summarizes")

RELATION_CN = {
    "defines": "定义",
    "states": "陈述",
    "proves": "证明",
    "motivates": "动机",
    "illustrates": "图示",
    "summarizes": "归纳",
}

# 通用类型（各学科模板都带）：定义/定理/动机/图示/延伸。
# textbook 是历史 profile 名（系统默认），与 math 同义共存——漏掉它会让默认模板变空集。
_COMMON = ("textbook", "math", "paper", "physics", "cs", "medicine", "chemistry", "custom")

REGISTRY: dict[str, BlockTypeSpec] = {
    # ---- 数学（教材默认，4.4.4 约束表）----
    "definition": BlockTypeSpec(
        "definition", "📌", "定义", "definition", "md_block.j2",
        "required", ("defines",), False, _COMMON,
        "定义：忠实转录定义内容（对象 + 判定条件），公式用 LaTeX"),
    "theorem": BlockTypeSpec(
        "theorem", "📘", "定理", "theorem", "md_block.j2",
        "required", ("states", "proves"), False, _COMMON,
        "定理：陈述 + 条件；原文有证明则概括证明思路，无证明不补造"),
    "motivation": BlockTypeSpec(
        "motivation", "💡", "动机", None, "md_block.j2",
        "optional", ("motivates",), True, _COMMON,
        "动机：说明引入动机与直觉，可综合归纳"),
    "figure": BlockTypeSpec(
        "figure", "🖼", "图示", "figure", "md_block.j2",
        "required", ("illustrates",), False, _COMMON,
        "图示：输出图片占位符、图注与解读文字"),
    "link": BlockTypeSpec(
        "link", "🔗", "延伸链接", None, "md_block.j2",
        "none", (), True, _COMMON,
        "延伸：跨章节/跨书的联系与延伸阅读，无来源"),
    # ---- 论文（5.8.3）----
    "contribution": BlockTypeSpec(
        "contribution", "🎯", "贡献", "contribution", "md_block.j2",
        "required", ("summarizes",), False, ("paper",),
        "贡献：论文的核心贡献点，忠实概括"),
    "method": BlockTypeSpec(
        "method", "🧭", "方法", "method", "md_block.j2",
        "required", ("defines", "states"), False, ("paper",),
        "方法：方法/流程的忠实描述"),
    "experiment": BlockTypeSpec(
        "experiment", "📊", "实验", "experiment", "md_block.j2",
        "required", ("illustrates", "summarizes"), False, ("paper",),
        "实验：实验设置与结果的忠实记录"),
    # ---- 物理 ----
    "law": BlockTypeSpec(
        "law", "⚖️", "定律", "theorem", "md_block.j2",
        "required", ("states", "proves"), False, ("physics",),
        "定律：定律陈述 + 适用条件与范围，公式用 LaTeX"),
    "phenomenon": BlockTypeSpec(
        "phenomenon", "🌌", "现象", None, "md_block.j2",
        "required", ("illustrates",), False, ("physics",),
        "现象：物理现象的观察描述与解释要点"),
    "phys_experiment": BlockTypeSpec(
        "phys_experiment", "🔬", "实验", "experiment", "md_block.j2",
        "required", ("illustrates", "summarizes"), False, ("physics",),
        "实验：实验装置、步骤与结论的忠实记录"),
    # ---- 计算机 ----
    "algorithm": BlockTypeSpec(
        "algorithm", "⚙️", "算法", None, "md_block.j2",
        "required", ("defines", "states"), False, ("cs",),
        "算法：算法步骤/伪代码的忠实描述（可用代码块）+ 复杂度（若原文给出）"),
    "data_structure": BlockTypeSpec(
        "data_structure", "🗂", "数据结构", "definition", "md_block.j2",
        "required", ("defines",), False, ("cs",),
        "数据结构：结构定义 + 关键操作与性质"),
    "complexity": BlockTypeSpec(
        "complexity", "⏱", "复杂度", None, "md_block.j2",
        "optional", ("states", "summarizes"), False, ("cs",),
        "复杂度：时间/空间复杂度结论与成立条件"),
    # ---- 医学 ----
    "symptom": BlockTypeSpec(
        "symptom", "🩺", "症状", None, "md_block.j2",
        "required", ("states", "illustrates"), False, ("medicine",),
        "症状：临床表现的忠实描述（部位/性质/伴随症状）"),
    "diagnosis": BlockTypeSpec(
        "diagnosis", "🔍", "诊断", "definition", "md_block.j2",
        "required", ("defines", "states"), False, ("medicine",),
        "诊断：诊断标准/要点与鉴别要点"),
    "treatment": BlockTypeSpec(
        "treatment", "💊", "治疗", None, "md_block.j2",
        "required", ("states", "summarizes"), False, ("medicine",),
        "治疗：治疗方案/用药与注意事项，剂量单位不得改动"),
    # ---- 化学 ----
    "substance": BlockTypeSpec(
        "substance", "🧪", "物质", "definition", "md_block.j2",
        "required", ("defines",), False, ("chemistry",),
        "物质：物质的结构/性质/制备要点"),
    "reaction": BlockTypeSpec(
        "reaction", "⚗️", "反应", "theorem", "md_block.j2",
        "required", ("states", "illustrates"), False, ("chemistry",),
        "反应：反应式（LaTeX/文本式）+ 条件与现象"),
    "chem_property": BlockTypeSpec(
        "chem_property", "🌀", "性质", None, "md_block.j2",
        "required", ("states",), False, ("chemistry",),
        "性质：物质性质的忠实描述与变化规律"),
}

# 用户自定义类型（config.generate.block_types → install_types 注入）：id → spec
_CUSTOM: dict[str, BlockTypeSpec] = {}

_TEX_ENVS = (None, "definition", "theorem", "figure", "experiment", "method", "contribution")


def parse_custom_types(custom: list[dict] | None) -> tuple[dict[str, BlockTypeSpec], list[str]]:
    """Build a candidate registry without changing the active definitions."""
    specs: dict[str, BlockTypeSpec] = {}
    bad: list[str] = []
    for item in custom or []:
        if not isinstance(item, dict):
            bad.append("?")
            continue
        try:
            tid = str(item.get("id") or "").strip()
            if not tid or not tid.replace("_", "").isalnum():
                bad.append(str(item.get("id") or "?"))
                continue
            policy = str(item.get("source_policy") or "required")
            if policy not in ("required", "optional", "none"):
                policy = "required"
            rels = tuple(r for r in (item.get("allowed_relations") or SOURCE_RELATIONS)
                         if r in SOURCE_RELATIONS)
            specs[tid] = BlockTypeSpec(
                tid,
                str(item.get("icon") or "📝"),
                str(item.get("name") or tid),
                item.get("tex_env") if item.get("tex_env") in _TEX_ENVS else None,
                "md_block.j2",
                policy,
                rels or SOURCE_RELATIONS,
                bool(item.get("synthesis_allowed")),
                # 未指定 profiles 的自定义类型对全部模板生效（"*" = 通配）
                tuple(item.get("profiles") or ("*",)),
                str(item.get("hint") or ""))
        except Exception:
            bad.append(str(item.get("id") or "?"))
    return specs, bad


def install_types(custom: list[dict] | None) -> list[str]:
    """Publish complete custom definitions; readers never see a half-built registry."""
    global _CUSTOM
    specs, bad = parse_custom_types(custom)
    _CUSTOM = specs
    return bad


def get_type(type_id: str) -> BlockTypeSpec:
    spec = _CUSTOM.get(type_id) or REGISTRY.get(type_id)
    if spec is None:
        raise SchemaViolation(f"未知块类型: {type_id}", code="UNKNOWN_BLOCK_TYPE")
    return spec


def active_types(profile: str = "textbook") -> list[BlockTypeSpec]:
    effective = {**REGISTRY, **_CUSTOM}
    specs = [s for s in effective.values()
             if profile in s.profiles or "*" in s.profiles]
    if not specs:
        # 未知/自定义 profile 名下的类型集不得为空：枚举空集 = 生成器什么都产不出，
        # 是静默哑火的悬崖；兜底回通用教材五类
        specs = [s for s in effective.values() if "textbook" in s.profiles or "*" in s.profiles]
    return specs


def type_enum(profile: str = "textbook") -> list[str]:
    return [s.type_id for s in active_types(profile)]


def type_guide(profile: str = "textbook") -> str:
    """生成提示词的类型写作指南：预设与自定义类型同一事实源。"""
    return "\n".join(f"- {s.type_id}（{s.cn_name}）：{s.hint}"
                     for s in active_types(profile) if s.hint)


def validate_type_constraints(type_id: str, *, basis: str, source_count: int) -> None:
    """规范 4.4.4 类型来源约束表（生成 prompt 与校验器共用）。"""
    spec = get_type(type_id)
    if spec.source_policy == "required" and source_count < 1:
        raise SchemaViolation(f"{type_id} 必须至少有一个来源锚点", code="SOURCE_REQUIRED")
    if spec.source_policy == "none" and source_count > 0:
        raise SchemaViolation(f"{type_id} 不要求来源，收到 {source_count} 个", code="SOURCE_NOT_ALLOWED")
    if basis == "synthesis" and not spec.synthesis_allowed:
        raise SchemaViolation(f"{type_id} 不允许 basis=synthesis", code="SYNTHESIS_NOT_ALLOWED")
    if basis == "anchored" and source_count < 1:
        raise SchemaViolation("basis=anchored 时 sources 必须非空", code="SOURCE_REQUIRED")


def outline_type_schema(profile: str = "textbook") -> dict:
    """生成 JSON Schema 的 type 枚举由注册表动态生成（附录 A 使用注 2）。"""
    return {"enum": type_enum(profile)}
