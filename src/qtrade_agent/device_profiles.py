"""内置设备身份档案库模板(05 §2.5.1;02 §3.4 #24 ``GET /device-profiles/templates`` 的数据源)。

字段口径以 **02 §3.1 `device_profiles` DDL** 为准:模板键叫 ``profile_key``(R6-60 (b):旧名 ``template_key`` 作废)。
``#24`` 出参逐字 = ``[{profile_key, brand, model, release, weight}]``;分配时另取本表的 ``manufacturer``/``device``/
``product``/``build_id``/``sdk``/``fingerprint_pattern``/``width``/``height``/``dpi`` 落 ``device_profiles`` 行。

⚠️ **本表是 #24 的最小可用数据源,不是完整档案库**:05 §2.5.1 要求库里有 30~50 个真实市售机型,
当前只登记了 10 条形状正确的条目。补齐由账号/runtime 实现方按同一形状追加,**端点无需改动**
(它只读本模块的 ``TEMPLATES``)。``weight`` = 分配时的相对权重(越大越常被抽中),与 05 §2.5.1 同义。
"""
from __future__ import annotations

from typing import Any

#: 模板全集。``fingerprint_pattern`` 里的 ``{serialno}`` 由分配方替换(本模块不生成 serialno/mac)。
TEMPLATES: tuple[dict[str, Any], ...] = (
    {"profile_key": "xiaomi_2211133c", "brand": "Xiaomi", "manufacturer": "Xiaomi", "model": "2211133C",
     "device": "fuxi", "product": "fuxi", "build_id": "TKQ1.220829.002", "release": "13", "sdk": 33,
     "fingerprint_pattern": "Xiaomi/fuxi/fuxi:13/TKQ1.220829.002/V14.0.4.0.TMACNXM:user/release-keys",
     "width": 1080, "height": 2400, "dpi": 440, "weight": 10},
    {"profile_key": "xiaomi_23013ra", "brand": "Redmi", "manufacturer": "Xiaomi", "model": "23013RK75C",
     "device": "sky", "product": "sky", "build_id": "TP1A.220624.014", "release": "13", "sdk": 33,
     "fingerprint_pattern": "Redmi/sky/sky:13/TP1A.220624.014/V14.0.2.0.TMKCNXM:user/release-keys",
     "width": 1080, "height": 2400, "dpi": 440, "weight": 8},
    {"profile_key": "huawei_ana_an00", "brand": "HUAWEI", "manufacturer": "HUAWEI", "model": "ANA-AN00",
     "device": "HWANA", "product": "ANA-AN00", "build_id": "HUAWEIANA-AN00", "release": "10", "sdk": 29,
     "fingerprint_pattern": "HUAWEI/ANA-AN00/HWANA:10/HUAWEIANA-AN00/102.0.0.229C00:user/release-keys",
     "width": 1080, "height": 2340, "dpi": 480, "weight": 7},
    {"profile_key": "honor_ele_an00", "brand": "HONOR", "manufacturer": "HUAWEI", "model": "ELE-AN00",
     "device": "HWELE", "product": "ELE-AN00", "build_id": "HUAWEIELE-AN00", "release": "10", "sdk": 29,
     "fingerprint_pattern": "HONOR/ELE-AN00/HWELE:10/HUAWEIELE-AN00/102.0.0.180C00:user/release-keys",
     "width": 1080, "height": 2340, "dpi": 480, "weight": 5},
    {"profile_key": "oppo_pgz110", "brand": "OPPO", "manufacturer": "OPPO", "model": "PGZ110",
     "device": "OP5929", "product": "PGZ110", "build_id": "SP1A.210812.016", "release": "13", "sdk": 33,
     "fingerprint_pattern": "OPPO/PGZ110/OP5929:13/SP1A.210812.016/Q_R.202305:user/release-keys",
     "width": 1080, "height": 2412, "dpi": 480, "weight": 7},
    {"profile_key": "vivo_v2227a", "brand": "vivo", "manufacturer": "vivo", "model": "V2227A",
     "device": "PD2227", "product": "PD2227", "build_id": "TP1A.220624.014", "release": "13", "sdk": 33,
     "fingerprint_pattern": "vivo/PD2227/PD2227:13/TP1A.220624.014/compiler05191359:user/release-keys",
     "width": 1080, "height": 2400, "dpi": 480, "weight": 7},
    {"profile_key": "oneplus_pjd110", "brand": "OnePlus", "manufacturer": "OnePlus", "model": "PJD110",
     "device": "OP594DL1", "product": "PJD110", "build_id": "TP1A.220905.001", "release": "13", "sdk": 33,
     "fingerprint_pattern": "OnePlus/PJD110/OP594DL1:13/TP1A.220905.001/Q_R.202306:user/release-keys",
     "width": 1240, "height": 2772, "dpi": 480, "weight": 4},
    {"profile_key": "samsung_sm_g9910", "brand": "samsung", "manufacturer": "samsung", "model": "SM-G9910",
     "device": "o1q", "product": "o1qzc", "build_id": "TP1A.220624.014", "release": "13", "sdk": 33,
     "fingerprint_pattern": "samsung/o1qzc/o1q:13/TP1A.220624.014/G9910ZCU4DWD1:user/release-keys",
     "width": 1080, "height": 2400, "dpi": 421, "weight": 3},
    {"profile_key": "meizu_m2291", "brand": "Meizu", "manufacturer": "Meizu", "model": "M2291",
     "device": "m2291", "product": "meizu_m2291_CN", "build_id": "SP1A.210812.016", "release": "12", "sdk": 31,
     "fingerprint_pattern": "Meizu/meizu_m2291_CN/m2291:12/SP1A.210812.016/1661242885:user/release-keys",
     "width": 1080, "height": 2340, "dpi": 440, "weight": 2},
    {"profile_key": "realme_rmx3708", "brand": "realme", "manufacturer": "realme", "model": "RMX3708",
     "device": "RE58B4", "product": "RMX3708", "build_id": "SP1A.210812.016", "release": "13", "sdk": 33,
     "fingerprint_pattern": "realme/RMX3708/RE58B4:13/SP1A.210812.016/S.202304:user/release-keys",
     "width": 1080, "height": 2412, "dpi": 480, "weight": 3},
)

#: #24 出参只给这四个描述键 + weight(02 §3.4 #24 逐字),其余列是分配时才用的细节
LIST_KEYS = ("profile_key", "brand", "model", "release", "weight")


def templates() -> list[dict[str, Any]]:
    """``#24`` 的列表视图:只回 02 §3.4 #24 定死的五个键。"""
    return [{k: t[k] for k in LIST_KEYS} for t in TEMPLATES]


def by_key(profile_key: str) -> dict[str, Any] | None:
    """按 ``profile_key`` 取完整模板(分配方用);不存在回 ``None``。"""
    return next((dict(t) for t in TEMPLATES if t["profile_key"] == profile_key), None)
