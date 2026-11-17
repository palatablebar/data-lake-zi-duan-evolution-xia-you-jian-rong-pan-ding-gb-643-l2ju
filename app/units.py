"""单位治理：单位登记、入口归一换算、幂等保护、单位差异判定。

裁定：上游字段的单位变更只在 M1 采信入口承担一次。
- 湖仓内部只流通标准单位（canonical unit），下游与页面永远不直接读上游原始单位的值。
- 原始单位、原始因子与换算依据随快照保留，旧版本回放按原值呈现，历史金额不丢。
- 换算带幂等标记：已经是标准单位的记录（历史回写 / 补数再次进入）直接跳过，绝不二次换算。

本模块只放事实与纯规则，不访问数据库；落库由 M1/M6 编排负责。
"""

from __future__ import annotations

from decimal import Decimal

# 湖仓金额标准单位：元。上游可能给元、角、分等，统一在入口归一到这里。
CANONICAL_UNIT = "CNY_YUAN"

# 单位登记：field_id -> 该字段在湖仓里的标准单位。
# 只有登记过的字段才允许进入单位治理；未登记的金额字段在采信时给阻断诊断。
# key 一律用字符串，与 field_id 的保形存储一致（大整数身份不过 float）。
GOVERNED_FIELDS: dict[str, str] = {
    "1004": CANONICAL_UNIT,  # dim_order.order_amt 订单金额
    "2003": CANONICAL_UNIT,  # fct_pay.pay_amt 支付金额
    "2006": CANONICAL_UNIT,  # fct_pay.pay_fee 手续费
}

# 上游原始单位 -> 换算成 1 个标准单位所需的原始单位数（factor）。
# 归一值 = 原始值 / factor。例如分 -> 元：123 分 / 100 = 1.23 元。
# 用 Decimal 常量，禁止 float，避免月末结算出现二进制尾差。
UNIT_FACTORS: dict[str, Decimal] = {
    "CNY_YUAN": Decimal("1"),        # 元（标准单位）
    "CNY_JIAO": Decimal("10"),       # 角
    "CNY_FEN": Decimal("100"),       # 分
    "CNY_LI": Decimal("1000"),       # 厘
}

# 单位别名：元数据里各种写法 -> 内部单位码。
UNIT_ALIASES: dict[str, str] = {
    "cny_yuan": "CNY_YUAN",
    "yuan": "CNY_YUAN",
    "rmb_yuan": "CNY_YUAN",
    "元": "CNY_YUAN",
    "人民币元": "CNY_YUAN",
    "cny_jiao": "CNY_JIAO",
    "jiao": "CNY_JIAO",
    "角": "CNY_JIAO",
    "cny_fen": "CNY_FEN",
    "fen": "CNY_FEN",
    "分": "CNY_FEN",
    "cny_li": "CNY_LI",
    "li": "CNY_LI",
    "厘": "CNY_LI",
}

UNIT_LABELS = {
    "CNY_YUAN": "元",
    "CNY_JIAO": "角",
    "CNY_FEN": "分",
    "CNY_LI": "厘",
}


def is_governed(field_id: str | None) -> bool:
    """该字段身份是否纳入单位治理（金额字段）。"""
    return field_id is not None and str(field_id) in GOVERNED_FIELDS


def canonical_unit_of(field_id: str | None) -> str | None:
    if field_id is None:
        return None
    return GOVERNED_FIELDS.get(str(field_id))


def normalize_unit_code(raw: str | None) -> str | None:
    """把元数据里的单位写法归一到内部单位码；不认识返回 None。"""
    if raw is None:
        return None
    token = str(raw).strip().lower()
    if not token:
        return None
    return UNIT_ALIASES.get(token)


def unit_label(code: str | None) -> str:
    if not code:
        return "—"
    return UNIT_LABELS.get(code, code)


def factor_of(unit_code: str) -> Decimal:
    """取单位相对标准单位的因子；未登记单位调用方应先判空，这里保守抛 KeyError。"""
    return UNIT_FACTORS[unit_code]


def describe_factor(unit_code: str, canonical: str = CANONICAL_UNIT) -> str:
    """人类可读的换算依据，例如 '分 → 元：原值 / 100'。"""
    factor = UNIT_FACTORS[unit_code]
    canonical_factor = UNIT_FACTORS[canonical]
    ratio = factor / canonical_factor
    if ratio == Decimal("1"):
        return f"{unit_label(unit_code)}即标准单位，无需换算"
    # to_integral_value 避免 Decimal.normalize() 把 100 显示成 1E+2。
    ratio_text = format(ratio.to_integral_value() if ratio == ratio.to_integral_value()
                        else ratio, "f")
    return f"{unit_label(unit_code)} → {unit_label(canonical)}：原值 / {ratio_text}"


def normalize_field_unit(field: dict, ruleset: str) -> dict:
    """对单个字段做入口单位归一，返回带单位治理信息的新字段（不修改入参）。

    幂等约定：字段已带 unit 治理块且 canonical 标记为真时，视为历史回写 / 补数
    再次进入，只复核单位一致性，不重复换算。
    """
    field_id = str(field.get("field_id") or "")
    out = dict(field)
    if not is_governed(field_id):
        return out

    canonical = GOVERNED_FIELDS[field_id]
    declared = normalize_unit_code(field.get("unit") or field.get("unit_raw"))

    # 幂等快路：该记录已带治理块（历史回写 / 补数重放）。
    # 治理块是入口换算的唯一凭据，重放时原样保留，绝不依据声明单位再算一次。
    prior = field.get("unit_governance")
    if isinstance(prior, dict) and prior.get("governed"):
        governance = dict(prior)
        governance["replayed"] = True
        if declared is not None and governance.get("source_unit") \
                and declared not in (governance["source_unit"],
                                     governance.get("canonical_unit")):
            governance["replay_conflict"] = (
                f"重放单位 {declared} 与登记原始单位 {governance['source_unit']} 不一致"
            )
        # 已是标准单位的补数：显式标记，converted 恒为 False，杜绝重复换算。
        if governance.get("canonical_unit") == declared or governance.get("already_canonical"):
            governance["already_canonical"] = True
            governance["converted"] = False
            governance["idempotency_code"] = "UNIT_ALREADY_CANONICAL"
        out["unit_governance"] = governance
        out["unit"] = governance.get("canonical_unit") or canonical
        return out

    if declared is None:
        # 金额字段没给可识别单位：历史口径默认就是湖仓标准单位（元），
        # 采信不阻断、不发诊断，但在治理块里留 assumed 标记；
        # 若后续版本从「显式单位」退化成「不标单位」，由 M3 的 diff_unit 判 UNIT_UNDECLARED。
        out["unit"] = canonical
        out["unit_governance"] = {
            "field_id": field_id,
            "governed": True,
            "declared_unit": None,
            "source_unit": None,
            "canonical_unit": canonical,
            "factor": str(UNIT_FACTORS[canonical]),
            "converted": False,
            "already_canonical": True,
            "undeclared": True,
            "assumed": True,
            "basis": "未显式登记单位，按湖仓标准单位（元）采信",
            "ruleset": ruleset,
        }
        return out

    factor = UNIT_FACTORS[declared]
    already = declared == canonical
    out["unit"] = canonical
    out["unit_governance"] = {
        "field_id": field_id,
        "governed": True,
        "declared_unit": declared,
        "source_unit": declared,
        "canonical_unit": canonical,
        "factor": str(factor),
        "converted": not already,
        "already_canonical": already,
        "undeclared": False,
        "basis": describe_factor(declared, canonical),
        "ruleset": ruleset,
    }
    return out


def convert_value(raw_value, unit_code: str, canonical: str = CANONICAL_UNIT):
    """按单位因子换算单个数值；仅用于演示 / 校验换算口径，判定不依赖具体数据值。"""
    if raw_value is None:
        return None
    value = Decimal(str(raw_value))
    ratio = UNIT_FACTORS[unit_code] / UNIT_FACTORS[canonical]
    return value / ratio


def diff_unit(old_field: dict, new_field: dict) -> dict | None:
    """M3 判定用：比较两个版本同一字段的单位，返回单位差异，无差异返回 None。

    - 两侧都未显式登记（历史口径默认元）：视为同单位，不报差异。
    - 旧版显式有单位、新版退化成不登记：无法确认量纲，判 UNIT_UNDECLARED（破坏）。
    - 显式单位不同（元 → 分等）：判 UNIT_SCALED（破坏），类型即使放宽也不放行。
    """
    old_gov = old_field.get("unit_governance")
    new_gov = new_field.get("unit_governance")
    if not (old_gov or new_gov):
        return None

    old_declared = (old_gov or {}).get("declared_unit")
    new_declared = (new_gov or {}).get("declared_unit")
    old_assumed = bool((old_gov or {}).get("assumed"))
    new_assumed = bool((new_gov or {}).get("assumed"))
    canonical = (new_gov or old_gov or {}).get("canonical_unit")

    # 旧版有明确单位，新版却不再登记：裸读风险，阻断。
    if old_declared and new_assumed:
        return {
            "changed": True,
            "reason": "UNIT_UNDECLARED",
            "old_unit": old_declared,
            "new_unit": None,
            "canonical_unit": canonical,
            "factor": None,
            "basis": "上游不再登记单位，无法确认量纲，禁止按原值裸读",
        }

    old_unit = old_declared or (old_gov or {}).get("canonical_unit")
    new_unit = new_declared or (new_gov or {}).get("canonical_unit")
    if old_unit != new_unit:
        return {
            "changed": True,
            "reason": "UNIT_SCALED",
            "old_unit": old_unit,
            "new_unit": new_unit,
            "canonical_unit": canonical,
            "factor": (new_gov or {}).get("factor"),
            "basis": (new_gov or {}).get("basis"),
        }
    return None


def unit_reason_codes() -> tuple[str, ...]:
    """单位治理引入的原因码，供原因码表与优先级登记复用。"""
    return ("UNIT_SCALED", "UNIT_UNDECLARED", "UNIT_ALREADY_CANONICAL", "UNIT_CONVERTED")
