"""两向判定：读者看名字与可解释性，写者看必填与容纳能力。

同一处变对对两边的影响并不相同，所以必须分开给结论，再合成一个总的。
"""

from __future__ import annotations


def required_change(old_required: bool, new_required: bool) -> str:
    if bool(old_required) == bool(new_required):
        return "same"
    if old_required and not new_required:
        return "loosened"
    return "tightened"


def reader(state: dict) -> dict:
    """读者：按旧名字、旧类型消费新数据。"""
    if state.get("table_changed"):
        return {"ok": False, "reason": "ID_REUSED", "repair": None}
    if state.get("conflict"):
        return {"ok": False, "reason": state.get("primary") or "RENAME_AND_RETYPE",
                "repair": "manual"}
    if state.get("deleted_referenced"):
        return {"ok": False, "reason": "FIELD_DELETED_REFERENCED", "repair": "keep_field"}
    if state.get("unit_changed"):
        # 读者按旧单位解释新数据会把金额放大/缩小，必须在入口换算后才放行。
        return {"ok": False, "reason": state.get("unit_reason") or "UNIT_SCALED",
                "repair": "convert_at_intake"}
    change = state.get("type_change")
    if change in ("narrow", "cross_family", "unknown"):
        return {"ok": False, "reason": state.get("primary") or "TYPE_NARROW", "repair": None}
    if state.get("rename"):
        return {"ok": True, "reason": "RENAME_ONLY", "repair": "alias"}
    return {"ok": True, "reason": "COMPATIBLE", "repair": None}


def writer(state: dict) -> dict:
    """写者：按新名字、新类型写入。"""
    if state.get("table_changed"):
        return {"ok": False, "reason": "ID_REUSED", "repair": None}
    if state.get("conflict"):
        return {"ok": False, "reason": state.get("primary") or "RENAME_AND_RETYPE",
                "repair": "manual"}
    if state.get("unit_changed"):
        # 写者若沿用旧口径写入，落库的量纲与新单位不一致，同样要求入口归一。
        return {"ok": False, "reason": state.get("unit_reason") or "UNIT_SCALED",
                "repair": "convert_at_intake"}
    change = state.get("type_change")
    if change in ("narrow", "cross_family", "unknown"):
        return {"ok": False, "reason": state.get("primary") or "TYPE_NARROW", "repair": None}
    if state.get("required_change") == "tightened":
        return {"ok": False, "reason": "REQUIRED_TIGHTENED", "repair": "default_value"}
    return {"ok": True, "reason": "COMPATIBLE", "repair": None}


def combine(reader_view: dict, writer_view: dict, renamed: bool) -> str:
    if not reader_view["ok"] or not writer_view["ok"]:
        return "BREAKING"
    if renamed:
        return "COMPATIBLE_BY_MAPPING"
    return "COMPATIBLE"
