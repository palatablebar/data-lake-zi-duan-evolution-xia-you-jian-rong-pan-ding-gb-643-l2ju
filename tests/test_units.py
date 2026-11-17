"""单位变更治理：入口唯一换算、幂等防重复、判定拦截、回放保原值、批次幂等。

场景：上游把订单/支付金额单位从元改成分（×100），类型仍是 decimal 甚至更宽。
朴素类型判定会把全部下游判兼容，月底结算金额放大百倍——本文件锁住这套防线。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import store, units
from app.modules.m1_intake import service as intake
from app.modules.m2_types import service as types
from app.modules.m3_compat import service as compat
from app.modules.m6_batches import service as batch_service
from app.modules.m7_migration import service as migration

OLD_UNIT_FILE = "schema_unit_v2026_10.sql"
NEW_UNIT_FILE = "schema_unit_v2026_11.sql"

GOVERNED_IDS = ("1004", "2003", "2006")


# ---------- 入口换算 ----------

def test_intake_converts_fen_to_yuan_once_at_entry(client):
    snapshot = types.prepare(intake.ingest(NEW_UNIT_FILE))
    for field_id in GOVERNED_IDS:
        field = next(f for f in snapshot["fields"] if f["field_id"] == field_id)
        gov = field["unit_governance"]
        assert gov["governed"] is True
        assert gov["source_unit"] == "CNY_FEN"
        assert gov["canonical_unit"] == "CNY_YUAN"
        assert gov["converted"] is True
        # 下游只能看到标准单位
        assert field["unit"] == "CNY_YUAN"
    # 换算留痕，但只是 info 级提示，不阻断采信
    assert any(row["reason"] == "UNIT_CONVERTED" for row in snapshot["diagnostics"])
    assert snapshot["unit_report"]["converted_fields"] == 3
    assert snapshot["unit_report"]["canonical_unit"] == "CNY_YUAN"


def test_conversion_factor_is_decimal_and_math_exact():
    # 123 分必须精确变成 1.23 元，不能有 float 尾差
    assert units.convert_value(123, "CNY_FEN") == Decimal("1.23")
    assert units.convert_value("100", "CNY_FEN") == Decimal("1")
    assert units.convert_value(5, "CNY_JIAO") == Decimal("0.5")
    assert isinstance(units.factor_of("CNY_FEN"), Decimal)


def test_old_version_keeps_original_unit_and_value_basis(client):
    """旧版本回放：元就是标准单位，不换算、不丢历史金额口径。"""
    snapshot = types.prepare(intake.ingest(OLD_UNIT_FILE))
    order_amt = next(f for f in snapshot["fields"] if f["field_id"] == "1004")
    gov = order_amt["unit_governance"]
    assert gov["source_unit"] == "CNY_YUAN"
    assert gov["converted"] is False
    assert gov["already_canonical"] is True
    # 历史快照里仍能读到原始登记单位
    assert order_amt["unit"] == "CNY_YUAN"
    assert not any(row["reason"] == "UNIT_CONVERTED" for row in snapshot["diagnostics"])


# ---------- 幂等：历史回写 / 补数不重复换算 ----------

def test_replay_governed_record_is_not_converted_twice(client):
    snapshot = types.prepare(intake.ingest(NEW_UNIT_FILE))
    field = next(f for f in snapshot["fields"] if f["field_id"] == "1004")
    # 模拟历史回写：同一条已治理记录再次进入入口
    replayed = units.normalize_field_unit(dict(field), "fm-2026.11.1")
    gov = replayed["unit_governance"]
    assert gov["replayed"] is True
    # 关键：因子仍是 100，但不会再除一次；converted 不再被当作新换算
    assert gov["factor"] == "100"
    assert gov["canonical_unit"] == "CNY_YUAN"
    assert replayed["unit"] == "CNY_YUAN"


def test_backfill_already_canonical_skips_conversion():
    field = {"field_id": "1004", "unit": "yuan"}
    gov = units.normalize_field_unit(field, "fm-2026.11.1")["unit_governance"]
    assert gov["already_canonical"] is True
    assert gov["converted"] is False
    # 带治理块的标准单位补数，显式幂等码
    with_prior = units.normalize_field_unit(
        {"field_id": "1004", "unit": "yuan", "unit_governance": dict(gov)},
        "fm-2026.11.1",
    )["unit_governance"]
    assert with_prior["idempotency_code"] == "UNIT_ALREADY_CANONICAL"
    assert with_prior["converted"] is False


def test_replay_unit_conflict_is_flagged_not_silently_rebased():
    gov_yuan = units.normalize_field_unit(
        {"field_id": "1004", "unit": "yuan"}, "fm-2026.11.1"
    )["unit_governance"]
    # 重放时却声明成分：不能悄悄按新单位再绑一次
    conflicted = units.normalize_field_unit(
        {"field_id": "1004", "unit": "fen", "unit_governance": dict(gov_yuan)},
        "fm-2026.11.1",
    )["unit_governance"]
    assert "replay_conflict" in conflicted


# ---------- 判定：单位变更必须判破坏，不能被类型兼容掩盖 ----------

def test_unit_change_is_breaking_even_though_type_widens(client):
    old = types.prepare(intake.ingest(OLD_UNIT_FILE))
    new = types.prepare(intake.ingest(NEW_UNIT_FILE))
    diff = compat.diff_snapshots(old, new)
    rescaled = [r for r in diff["rows"] if r["bucket"] == "rescaled"]
    assert {r["field_id"] for r in rescaled} == set(GOVERNED_IDS)
    for row in rescaled:
        assert row["verdict"] == "BREAKING"
        assert row["primary"] == "UNIT_SCALED"
        assert row["reader_ok"] is False and row["writer_ok"] is False
        assert row["old_unit"] == "CNY_YUAN" and row["new_unit"] == "CNY_FEN"
        assert row["canonical_unit"] == "CNY_YUAN"
        assert "/ 100" in (row["unit_basis"] or "")
    # 单位变更计入破坏总数
    assert diff["counts"]["breaking"] >= 3


def test_unit_scaled_outranks_type_widen_in_priority():
    from app.modules.m3_compat import priority

    assert priority.rank("UNIT_SCALED") < priority.rank("TYPE_WIDEN")
    assert priority.primary(["TYPE_WIDEN", "UNIT_SCALED"]) == "UNIT_SCALED"


def test_undeclared_unit_after_explicit_one_is_breaking(client, tmp_path, monkeypatch):
    """旧版显式标了单位，新版退化成不登记：无法确认量纲，判 UNIT_UNDECLARED。"""
    old = types.prepare(intake.ingest(OLD_UNIT_FILE))
    undeclared_gov_field = {
        "field_id": "1004", "table": "dim_order", "name": "order_amt",
        "name_key": "order_amt", "type_raw": "decimal(18,2)", "required": True,
        "line": 1, "version": "vX", "source": "t", "unit": None,
    }
    new_field = units.normalize_field_unit(undeclared_gov_field, "fm-2026.11.1")
    old_field = next(f for f in old["fields"] if f["field_id"] == "1004")
    verdict = units.diff_unit(old_field, new_field)
    assert verdict is not None
    assert verdict["reason"] == "UNIT_UNDECLARED"


def test_historical_files_without_unit_stay_quiet(client):
    """既有历史样例没有单位标注：默认按元，不产生单位破坏、不发换算诊断。"""
    old = types.prepare(intake.ingest("schema_v2026_06.sql"))
    new = types.prepare(intake.ingest("schema_v2026_09.sql"))
    diff = compat.diff_snapshots(old, new)
    assert not any(r["bucket"] == "rescaled" for r in diff["rows"])
    for snapshot in (old, new):
        assert not any(x["reason"] == "UNIT_CONVERTED" for x in snapshot["diagnostics"])


# ---------- 批次：一份结论、重跑复用、回滚保留原值 ----------

def test_unit_batch_is_idempotent_and_keeps_single_conclusion(client):
    first = batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    run_id = first["run_id"]
    count_after_first = store.count_runs()
    again = batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    assert again["run_id"] == run_id
    assert again.get("reused") is True
    # 整批再跑不新增行，仍只有该输入对应的一份结论
    assert store.count_runs() == count_after_first
    assert migration.fields_csv_text(again) == migration.fields_csv_text(first)


def test_unit_governance_block_carries_release_and_basis(client):
    run = batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    ug = run["unit_governance"]
    assert ug["owner_stage"] == "M1_intake"
    assert ug["canonical_unit"] == "CNY_YUAN"
    assert ug["rescaled_count"] == 3
    assert ug["release"] == "BLOCKED"
    assert {item["field_id"] for item in ug["rescaled"]} == set(GOVERNED_IDS)


def test_manifest_and_exports_carry_unit_basis(client):
    run = batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    import json

    manifest = json.loads(migration.manifest_json_text(run))
    assert manifest["unit_governance"]["release"] == "BLOCKED"
    assert manifest["unit_governance"]["rescaled_count"] == 3

    spec = migration.spec_markdown(run)
    assert "UNIT_SCALED" in spec
    assert "/ 100" in spec
    assert "单位" in spec and "入口" in spec

    fields_csv = migration.fields_csv_text(run)
    assert "canonical_unit" in fields_csv and "CNY_FEN" in fields_csv
    # 原始单位与标准单位都在导出里，历史口径可追溯
    assert "CNY_YUAN" in fields_csv


def test_rollback_to_previous_version_preserves_raw_unit(client):
    """切换未完成回上一版：旧快照仍是元口径，重放不换算历史金额。"""
    new = types.prepare(intake.ingest(NEW_UNIT_FILE))
    old = types.prepare(intake.ingest(OLD_UNIT_FILE))
    # 回滚后读到的旧版本金额字段保持原始单位（元），无 converted 标记
    old_amt = next(f for f in old["fields"] if f["field_id"] == "1004")
    assert old_amt["unit_governance"]["source_unit"] == "CNY_YUAN"
    assert old_amt["unit_governance"]["converted"] is False
    # 新版本快照独立保留分->元的换算依据，互不污染
    new_amt = next(f for f in new["fields"] if f["field_id"] == "1004")
    assert new_amt["unit_governance"]["source_unit"] == "CNY_FEN"
    assert old_amt["unit_governance"] != new_amt["unit_governance"]


def test_unit_change_propagates_to_impact_as_breaking(client):
    run = batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    # 单位破坏项进入影响面（order_amt / pay_amt 被下游引用）
    impacted_ids = set()
    for item in run["unit_governance"]["rescaled"]:
        if item["reason"] == "UNIT_SCALED":
            impacted_ids.add(item["field_id"])
    assert "1004" in impacted_ids and "2003" in impacted_ids
    # 账目守恒不被新桶打破
    fields = run["ledger"]["fields"]
    assert fields["old_gap"] == 0 and fields["new_gap"] == 0


@pytest.mark.parametrize("raw,expected", [
    ("fen", "CNY_FEN"), ("分", "CNY_FEN"), ("yuan", "CNY_YUAN"),
    ("元", "CNY_YUAN"), ("jiao", "CNY_JIAO"), ("UNKNOWN_UNIT", None),
])
def test_unit_alias_normalization(raw, expected):
    assert units.normalize_unit_code(raw) == expected


def test_unit_governance_api_exposes_single_source_of_truth(client):
    batch_service.execute(
        old_file=OLD_UNIT_FILE, new_file=NEW_UNIT_FILE,
        refs_file="refs_downstream.json", base_file=None,
    )
    response = client.get("/api/units/governance")
    assert response.status_code == 200
    body = response.json()
    assert body["owner_stage"] == "M1_intake"
    assert body["canonical_unit"] == "CNY_YUAN"
    assert set(GOVERNED_IDS) <= set(body["governed_field_ids"])
    assert body["unit_factors"]["CNY_FEN"] == "100"
    assert body["reason_codes"]["UNIT_SCALED"]["severity"] == "break"
    assert body["batch"]["release"] == "BLOCKED"
    assert body["batch"]["rescaled_count"] == 3
