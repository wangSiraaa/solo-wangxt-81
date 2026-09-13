"""内置教学演示场景（payload 即 ScenarioIn 的 JSON 结构，可直接 POST /validate）。

所有数字都是虚构的教学数据，不代表任何真实水库，不用于现实供水决策。
"""
from __future__ import annotations

import copy

# 一座小型教学水库：死水位 820m / 死库容 200万 m³，
# 正常蓄水位 840m / 库容 2016万 m³。
def _curve():
    return [
        {"elevation": {"value": 820, "unit": "m"}, "storage": {"value": 200, "unit": "万m3"}, "area": {"value": 100_000, "unit": "m2"}},
        {"elevation": {"value": 825, "unit": "m"}, "storage": {"value": 600, "unit": "万m3"}, "area": {"value": 300_000, "unit": "m2"}},
        {"elevation": {"value": 830, "unit": "m"}, "storage": {"value": 1050, "unit": "万m3"}, "area": {"value": 500_000, "unit": "m2"}},
        {"elevation": {"value": 835, "unit": "m"}, "storage": {"value": 1500, "unit": "万m3"}, "area": {"value": 700_000, "unit": "m2"}},
        {"elevation": {"value": 840, "unit": "m"}, "storage": {"value": 2016, "unit": "万m3"}, "area": {"value": 900_000, "unit": "m2"}},
    ]


def _bounds():
    return {
        "dead_storage": {"value": 200, "unit": "万m3"},
        "max_storage": {"value": 2016, "unit": "万m3"},
        "initial_storage": {"value": 800, "unit": "万m3"},
    }


def _demands(scale=1.0):
    base_life, base_ind, base_agr = 30, 45, 120  # 万 m³/日
    n = 30
    agr = []
    for t in range(n):  # 农业灌溉中期高峰
        f = 1.0 if 8 <= t <= 22 else 0.55
        agr.append({"value": round(base_agr * scale * f, 2), "unit": "万m3"})
    return [
        {"key": "life", "name": "生活用水", "weight": 3.0,
         "amount": [{"value": base_life * scale, "unit": "万m3"}] * n,
         "min_fraction": 0.9},
        {"key": "industry", "name": "工业用水", "weight": 2.0,
         "amount": [{"value": base_ind * scale, "unit": "万m3"}] * n,
         "min_fraction": 0.0},
        {"key": "agriculture", "name": "农业灌溉", "weight": 1.0,
         "amount": agr, "min_fraction": 0.0},
    ]


def _normal_steps():
    # 30 天，入流万m³/日（=m³/d 口径的流量也可，这里直接给体积）
    pattern = [150, 140, 130, 120, 110, 105, 100, 95, 90, 88,
               90, 95, 110, 130, 160, 190, 170, 150, 140, 130,
               120, 115, 110, 105, 100, 95, 90, 88, 85, 80]
    steps = []
    for t, q in enumerate(pattern):
        steps.append({
            "label": f"D{t+1}",
            "duration": {"value": 1, "unit": "d"},
            "inflow": {"value": q, "unit": "万m3"},
            "net_evap_depth": {"value": 2.5, "unit": "mm"},
            "eco_flow": {"value": 20, "unit": "万m3"},
        })
    return steps


def scenario_base():
    p = {"name": "常规来水（基线）",
         "description": "30 天枯退来水；生活权重3/工业2/农业1。观察期末蓄水与农业缺水。"}
    p.update(_bounds())
    p["curve"] = _curve()
    p["steps"] = _normal_steps()
    p["demands"] = _demands()
    return p


def scenario_zero_inflow():
    p = {"name": "零来水（连旱）",
         "description": "30 天入流全为 0（真实的零，不是缺测）。看库存消耗、权重配水与生态底线冲突。"}
    p.update(_bounds())
    p["curve"] = _curve()
    steps = []
    for t in range(30):
        steps.append({
            "label": f"D{t+1}",
            "duration": {"value": 1, "unit": "d"},
            "inflow": {"value": 0, "unit": "万m3"},
            "net_evap_depth": {"value": 3.0, "unit": "mm"},
            "eco_flow": {"value": 15, "unit": "万m3"},
        })
    p["steps"] = steps
    p["demands"] = _demands()
    return p


def scenario_flood():
    p = {"name": "洪峰超过可弃空间",
         "description": "D10 来水 4200 万 m³，溢洪道单步最多 2400 万 m³；"
                        "即使 D9 预先弃水腾库，D10 可容纳空间+可弃水量仍小于洪峰，"
                        "冲突表应指出 D10 为 flood。"}
    b = _bounds()
    b["spillway_capacity"] = {"value": 2400, "unit": "万m3"}
    p.update(b)
    p["curve"] = _curve()
    steps = _normal_steps()
    for t, q in ((9, 4200), (10, 3000)):
        steps[t]["inflow"] = {"value": q, "unit": "万m3"}
    p["steps"] = steps
    p["demands"] = _demands()
    return p


def scenario_mixed_units():
    """同一个场景故意混用 m³/s、mm、亩、h、l/s 等单位，验证换算层。"""
    p = {"name": "混合单位（先换算再算账）",
         "description": "入流用 m³/s、生态用 l/s、蒸发 mm、时长 h、农业给 m³/d；"
                        "结果应与基线同口径可比较。"}
    b = _bounds()
    # 死库容/最大库容用 m³ 表示
    b["dead_storage"] = {"value": 2_000_000, "unit": "m3"}
    b["max_storage"] = {"value": 20_160_000, "unit": "m3"}
    b["initial_storage"] = {"value": 8_000_000, "unit": "m3"}
    p.update(b)
    curve = []
    for c in _curve():
        curve.append({
            "elevation": c["elevation"],
            "storage": {"value": c["storage"]["value"] * 10_000, "unit": "m3"},
            "area": c["area"],
        })
    p["curve"] = curve

    pattern = [150, 140, 130, 120, 110, 105, 100, 95, 90, 88,
               90, 95, 110, 130, 160, 190, 170, 150, 140, 130,
               120, 115, 110, 105, 100, 95, 90, 88, 85, 80]
    steps = []
    for t, q_wan in enumerate(pattern):
        m3s = q_wan * 10_000 / 86400  # 万m³/日 -> m³/s
        steps.append({
            "label": f"D{t+1}",
            "duration": {"value": 24, "unit": "h"},
            "inflow": {"value": m3s, "unit": "m3/s"},
            "net_evap_depth": {"value": 2.5, "unit": "mm"},
            "eco_flow": {"value": 20 * 10_000 / 86400 / 1e-3, "unit": "l/s"},
        })
    p["steps"] = steps
    # 生活/工业/农业需求量仍以体积给（万m³）；入流(m³/s)、生态(l/s)、时长(h)混用
    p["demands"] = _demands()
    return p


def scenario_missing():
    p = {"name": "来水缺测（不能当零）",
         "description": "D5、D6 来水缺测。直接求解会被 422 拒绝并列出缺测时段；"
                        "选择 linear 插补后可解，图表上有插补标记。"}
    p.update(_bounds())
    p["curve"] = _curve()
    steps = _normal_steps()
    steps[4]["inflow"] = {"value": None, "unit": "万m3"}
    steps[5]["inflow"] = {"value": None, "unit": "万m3"}
    p["steps"] = steps
    p["demands"] = _demands()
    return p


BUILTINS: dict[str, dict] = {
    "base": scenario_base,
    "zero": scenario_zero_inflow,
    "flood": scenario_flood,
    "mixed": scenario_mixed_units,
    "missing": scenario_missing,
}


def get_builtin(key: str) -> dict | None:
    fn = BUILTINS.get(key)
    return copy.deepcopy(fn()) if fn else None


def all_builtins() -> dict[str, dict]:
    return {k: copy.deepcopy(v()) for k, v in BUILTINS.items()}
