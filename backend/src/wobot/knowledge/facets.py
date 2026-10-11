"""Product facets (DEC-069): what the catalog states of each product, in a fixed vocabulary.

A recommendation filters products by facet instead of having a model judge each one per
turn: what a product does (`functions`), its form, where it is used, how it senses, where
its alerts are seen, its power and how many use it at once. A value is kept only with the
piece of the product's catalog text that states it; a facet the text says nothing of is
empty, and code reads an empty facet as unstated, never as a no.

The values live here, not in the database, which checks only the facet names: a value
added or renamed changes what the extraction asks, so it bumps that prompt's version
rather than needing a migration.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# The facets besides `functions`, each with the values a product may have; one product may
# have several, such as a worn pendant and a base unit in the room.
FACET_VALUES: dict[str, tuple[str, ...]] = {
    # worn: on the person cared for, not on staff; bed: the mattress, or a pad on or under
    # it; room: on a ceiling, wall or bedside, or a base unit; device: used only to measure.
    "form": ("worn", "bed", "room", "device", "equipment", "software", "service", "robot"),
    # Wards, beds or nurses alone name no place (2026-10-08).
    "places": ("home", "institution", "hospital", "public"),
    # camera includes images and 3D depth sensing; a plain "sensor" names no kind.
    "sensing": ("camera", "radar", "wifi", "pressure", "fiber", "infrared"),
    # Where a carer sees the data or the alert; computer includes web pages and central
    # monitoring software (2026-10-08), while a "platform" alone names none.
    "viewing": ("app", "computer", "line", "sms", "phone", "device"),
    "power": ("mains", "battery"),
    # For activities only, such as exercise: several at once, or a team competing.
    "users": ("group", "individual"),
}


@dataclass(frozen=True)
class Function:
    """Something a product does, with the words users and the catalog say it in, and the
    nearby functions it is not."""

    name: str
    aliases: tuple[str, ...] = ()
    unlike: tuple[str, ...] = ()


FUNCTIONS: tuple[Function, ...] = (
    # Safety and monitoring
    Function("跌倒偵測", ("偵測跌倒", "跌倒警報", "跌倒檢測", "跌倒自動感測"), ("跌倒預防",)),
    Function("跌倒預防", ("預防跌倒", "跌倒風險", "跌倒風險評估"), ("跌倒偵測",)),
    Function(
        "離床偵測", ("離床預警", "離床警示", "離床提醒", "離床通知", "在床狀態"), ("睡眠監測",)
    ),
    Function("翻身監測", ("翻身提醒", "久臥提醒", "翻身次數")),
    Function("壓瘡預防", ("壓瘡", "壓傷", "褥瘡", "均壓")),
    Function("呼吸監測", ("呼吸偵測", "呼吸頻率")),
    Function("心率監測", ("心跳", "心率"), ("心電圖",)),
    Function("睡眠監測", ("睡眠品質", "睡眠分析", "睡眠呼吸中止"), ("離床偵測",)),
    Function("滯留偵測", ("浴廁滯留", "廁所久留", "異常駐留", "停留逾時")),
    Function("活動監測", ("房內動態", "日常活動", "行為分析", "生活習慣追蹤")),
    Function("緊急求助", ("SOS", "緊急按鈕", "一鍵求助", "緊急通報", "呼救")),
    Function("定位", ("室內定位", "GPS", "即時位置")),
    Function("防走失", ("電子圍籬", "守護範圍", "走失")),
    Function("雙向通話", ("對講", "視訊通話")),
    Function("環境安全", ("火災", "瓦斯", "淹水")),
    Function("空氣品質監測", ("空氣品質", "PM2.5", "空汙")),
    Function("空氣淨化", ("淨化", "除菌")),
    Function("門禁", ("人臉辨識門禁",)),
    Function("醫療廢棄物處理", ("醫療廢棄物",)),
    # Measurement and health management
    Function("血壓量測", ("血壓", "血壓計")),
    Function("血糖量測", ("血糖", "連續血糖")),
    Function("血氧量測", ("血氧", "血氧濃度")),
    Function("體溫量測", ("體溫", "額溫")),
    Function("心電圖", ("ECG", "心律"), ("心率監測",)),
    Function("體組成", ("體脂", "肌肉量")),
    Function("血脂量測", ("血脂", "膽固醇")),
    Function("心血管檢測", ("心血管", "頸動脈")),
    Function("遠距健康管理", ("健康數據雲端", "健康紀錄", "遠距照護平台")),
    Function("遠距醫療", ("遠距診療", "遠距醫療推車")),
    Function("用藥提醒", ("服藥提醒", "用藥管理", "智慧藥櫃")),
    # Services and institutions
    Function("照顧服務媒合", ("居家照顧預約", "照顧服務平台")),
    Function("交通接送", ("接送", "長照專車")),
    Function("送餐", ("送餐管理",)),
    Function("機構管理", ("住民管理", "護理紀錄", "病歷紀錄", "排班", "核銷", "派工")),
    # Body and mind
    Function(
        "運動訓練",
        ("肌力", "健身", "肌耐力", "體能", "核心訓練", "行動訓練", "抬腿"),
        ("復健訓練",),
    ),
    Function("上肢復健", ("上肢訓練",)),
    Function("手部復健", ("手部", "握力", "手指")),
    Function("下肢復健", ("下肢", "行走訓練", "外骨骼")),
    Function("吞嚥訓練", ("吞嚥", "口部肌力", "咽部肌力")),
    Function("綜合復健", ("復健治療組",)),
    Function("認知訓練", ("記憶力", "專注力", "益智", "腦力", "反應訓練"), ("認知評估",)),
    Function("認知評估", ("腦年齡", "認知檢測"), ("認知訓練",)),
    Function("體適能檢測", ("體適能",)),
    Function("步態分析", ("步態",)),
    Function("情緒紓壓", ("放鬆", "安撫", "紓壓")),
    # Other
    Function("失禁照護", ("尿布", "自動清洗")),
    Function("配送機器人", ("配送", "送餐機器人")),
    Function("巡邏機器人", ("巡邏",)),
    Function("陪伴互動", ("陪伴機器人", "情感互動", "溝通機器人", "導覽")),
)

FUNCTION_NAMES = tuple(function.name for function in FUNCTIONS)

# A need named by a group is met by any of its functions: "復健" alone takes every kind.
FUNCTION_GROUPS: dict[str, tuple[str, ...]] = {
    "復健訓練": ("上肢復健", "手部復健", "下肢復健", "吞嚥訓練", "綜合復健"),
}

FACETS = ("functions", *FACET_VALUES)

# The catalog text a value's span may be read from, in the order it is looked for.
FACET_SOURCE_FIELDS = ("features_text", "usage_text", "summary_text")


@dataclass(frozen=True)
class FacetValue:
    """One value a product's text states, with the span that states it and its field."""

    facet: str
    value: str
    span: str
    source_field: str


def allowed_values(facet: str) -> tuple[str, ...]:
    """The values a facet may have; KeyError for a facet there is not."""
    return FUNCTION_NAMES if facet == "functions" else FACET_VALUES[facet]


# --- Judging a product against a need ---------------------------------------------------

Status = Literal["supported", "contradicted", "unknown"]


@dataclass(frozen=True)
class FacetQuery:
    """What a fitting product must have: every function, and per facet the values it must
    have and must not have. A function may be a group, met by any of its functions."""

    functions: tuple[str, ...] = ()
    must: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    must_not: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> "FacetQuery":
        """A query as a dataset writes it; ValueError for a word outside the vocabulary."""
        if unknown := sorted(set(data) - {"functions", "must", "must_not"}):
            raise ValueError(f"no query part {', '.join(unknown)}")
        functions = tuple(data.get("functions") or ())
        if outside := [
            f for f in functions if f not in FUNCTION_NAMES and f not in FUNCTION_GROUPS
        ]:
            raise ValueError(f"not functions: {outside}")
        conditions = {}
        for part in ("must", "must_not"):
            conditions[part] = {}
            for facet, values in (data.get(part) or {}).items():
                if facet not in FACET_VALUES:
                    raise ValueError(f"{part}: no facet {facet!r}")
                if outside := [v for v in values if v not in FACET_VALUES[facet]]:
                    raise ValueError(f"{part}: not {facet} values: {outside}")
                conditions[part][facet] = tuple(values)
        return cls(functions, conditions["must"], conditions["must_not"])


def status(values: Collection[str], value: str, *, must: bool) -> Status:
    """One condition on a facet (DEC-069): a facet the text states nothing of is unknown;
    else the value it must have is supported when stated and contradicted when not, and
    the value it must not have the other way round."""
    if not values:
        return "unknown"
    return "supported" if (value in values) == must else "contradicted"


def has_function(functions: Collection[str], name: str) -> bool:
    return any(f in functions for f in FUNCTION_GROUPS.get(name, (name,)))


def passes(facets: Mapping[str, Collection[str]], query: FacetQuery) -> bool:
    """Whether a product fits: every function stated, and no condition contradicted."""
    functions = facets.get("functions", ())
    if not all(has_function(functions, name) for name in query.functions):
        return False
    conditions = [
        status(facets.get(facet, ()), value, must=must)
        for must, part in ((True, query.must), (False, query.must_not))
        for facet, values in part.items()
        for value in values
    ]
    return "contradicted" not in conditions
