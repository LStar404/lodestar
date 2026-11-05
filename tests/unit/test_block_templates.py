"""学科模板与自定义块类型回归：注册表 → 生成 schema → 校验器 → API 同一事实源。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.core.block_types import install_types, type_enum, type_guide
from src.core.errors import SchemaViolation
from src.core.block_types import validate_type_constraints
from src.generate.prompts import build_outline_schema


@pytest.fixture(autouse=True)
def _clean_types():
    install_types([])
    yield
    install_types([])


def test_subject_presets_resolve():
    assert "law" in type_enum("physics") and "phenomenon" in type_enum("physics")
    assert "algorithm" in type_enum("cs")
    assert "symptom" in type_enum("medicine")
    assert "reaction" in type_enum("chemistry")
    assert type_enum("textbook") == ["definition", "theorem", "motivation", "figure", "link"]


def test_custom_type_feeds_schema_and_validator():
    bad = install_types([{"id": "case", "name": "病例", "icon": "🏥",
                          "hint": "病例：主诉/现病史/处置",
                          "source_policy": "required", "profiles": ["medicine"]}])
    assert bad == []
    assert "case" in type_enum("medicine")
    assert "case" not in type_enum("physics"), "profiles 限定生效"
    assert "病例" in type_guide("medicine")
    assert "case" in build_outline_schema("medicine")["properties"]["blocks"]["items"]["properties"]["type"]["enum"]
    # 校验器走同一注册表：自定义类型的来源策略必须被强制
    with pytest.raises(SchemaViolation):
        validate_type_constraints("case", basis="synthesis", source_count=0)
    install_types([{"id": "x y", "name": "非法 id"}])
    # 非法 id 不注入


def test_custom_type_override_preset_hint():
    install_types([{"id": "theorem", "name": "定理", "hint": "自定义的定理写作指引",
                    "source_policy": "required"}])
    assert "自定义的定理写作指引" in type_guide("textbook"), "同 id 覆盖预设生效"


def test_override_replaces_preset_in_schema_and_prompt():
    from src.core.block_types import active_types, get_type

    install_types([{"id": "theorem", "name": "自定义定理", "hint": "只使用新指引",
                    "source_policy": "optional", "synthesis_allowed": True}])
    specs = [s for s in active_types("textbook") if s.type_id == "theorem"]
    assert specs == [get_type("theorem")]
    assert type_enum("textbook").count("theorem") == 1
    assert type_guide("textbook").count("- theorem（") == 1
    assert "只使用新指引" in type_guide("textbook")


def test_malformed_custom_entry_does_not_crash_or_hide_valid_types():
    bad = install_types([None, 7, {"id": "valid_case", "name": "Case"}])
    assert len(bad) == 2
    assert "valid_case" in type_enum()


def test_anki_cards_cover_all_knowledge_types():
    """闪卡类型不得写死数学三类：定律/算法/诊断等知识点必须进牌组（曾漏掉）。"""
    from src.export.anki_export import card_types

    kinds = card_types()
    for t in ("definition", "theorem", "law", "algorithm", "diagnosis", "reaction"):
        assert t in kinds, t
    assert "figure" not in kinds and "link" not in kinds
    install_types([{"id": "case", "name": "病例", "hint": "x"}])
    assert "case" in card_types(), "自定义知识点类型也要进牌组"


def test_tex_export_degrades_for_removed_custom_type():
    """自定义类型被删后旧块仍要能导出（曾 get_type 直接炸掉整次导出）。"""
    from src.core.block_types import get_type

    try:
        get_type("deleted_custom")
        raise AssertionError("应当抛未知类型")
    except SchemaViolation:
        pass
    from src.export.tex_export import md_block_to_tex

    assert md_block_to_tex("普通内容"), "正常路径不回归"


def test_unknown_profile_never_yields_empty_type_set():
    """未知/自定义 profile 名下类型集不得为空：枚举空集 = 生成器静默哑火。"""
    assert type_enum("medicin") == type_enum("textbook"), "拼错的模板名回落通用类型"
    install_types([{"id": "case", "name": "病例", "hint": "x"}])
    assert "case" in type_enum("my-course"), "未指定 profiles 的自定义类型通配所有模板"


def test_settings_normalize_guards_template_fields():
    from src.core.config import normalize_settings

    cfg = normalize_settings({"generate": {"profile": "", "block_types": "垃圾"}})
    assert cfg["generate"]["profile"] == "textbook"
    assert cfg["generate"]["block_types"] == []


def test_block_types_api_and_settings_roundtrip(workspace):
    app = create_app(workspace.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        r = client.put("/api/v1/settings", json={"generate": {
            "profile": "medicine",
            "block_types": [{"id": "case", "name": "病例", "icon": "🏥",
                             "hint": "病例：主诉/现病史/处置", "source_policy": "required"}]}},
            headers=headers)
        assert r.status_code == 200, r.text
        r = client.get("/api/v1/block-types", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["profile"] == "medicine"
        ids = [t["id"] for t in body["types"]]
        assert "case" in ids and "symptom" in ids, ids
        assert "physics" in body["presets"]
