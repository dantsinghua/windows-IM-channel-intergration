"""设备身份档案库(05 §2.5.1;02 §3.4 #24 ``GET /device-profiles/templates`` 的数据源)。

**单一来源 = 随包落盘的 JSON 文件**(05 §2.5.1「随 Agent 包内置 ``device_profiles.json``,只读」),
路径经配置 ``[device_profiles] library``(05 §7 / 07 §[device_profiles],默认 ``/opt/qtrade/agent/data/device_profiles.json``)。
文件缺失 / 不是合法 JSON / 里面一条模板都没有 ⇒ **回落到本模块内置的一份小清单并记 ERROR**(不拒绝启动:
档案库只在「新建企点账号」时才需要,为它拒启动会连带砍掉全部其它通道)。

字段口径以 **02 §3.1 `device_profiles` DDL** 为准:模板键叫 ``profile_key``(R6-60 (b):旧名 ``template_key`` 作废)。
``#24`` 出参逐字 = ``[{profile_key, brand, model, release, weight}]``;分配时另取 ``manufacturer`` / ``device`` /
``product`` / ``build_id`` / ``sdk`` / ``fingerprint``(或 ``fingerprint_pattern``)/ ``width`` / ``height`` / ``dpi`` /
``serialno_style`` 落 ``device_profiles`` 行。

⚠️ 库文件里的条目**可以没有 ``weight``**(`installer/rootfs/profiles/device_profiles.json` 的 32 条就没有)——
缺省补 ``DEFAULT_WEIGHT``,这样 05 §2.5.1「按已用次数最少随机」与 #24 的出参形状都成立。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

log = logging.getLogger("qtrade.device_profiles")

#: #24 出参只给这四个描述键 + weight(02 §3.4 #24 逐字),其余列是分配时才用的细节
LIST_KEYS = ("profile_key", "brand", "model", "release", "weight")
DEFAULT_WEIGHT = 1                       # 库文件没写 weight 时的权重(等权随机)

#: 🔴 **回落清单,不是档案库**:库文件读不到时才用。05 §2.5.1 要求真库里有 30~50 个真实市售机型
#: (`installer/rootfs/profiles/device_profiles.json` 现 32 条);这里只留 10 条形状正确的保底条目,
#: 好让「库丢了」表现成「能建号但机型池很小 + 一条 ERROR 日志」,而不是「建号直接炸」。
BUILTIN_FALLBACK: tuple[dict[str, Any], ...] = (
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

SOURCE_LIBRARY = "library"
SOURCE_BUILTIN = "builtin_fallback"


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def parse_library(raw: str) -> list[dict[str, Any]]:
    """解析库文件正文 → 模板列表。形状不对 ⇒ ``ValueError``(调用方据此回落,不静默吞)。

    认两种顶层形状:``{"templates":[…]}``(installer 落盘的那份)与裸数组。每条须有非空 ``profile_key``;
    旧名 ``template_key`` 一律**拒收**(R6-60 (b) 作废名,静默接受等于把旧名养活)。
    """
    doc = json.loads(raw)
    rows = doc.get("templates") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ValueError("顶层既不是 {templates:[…]} 也不是数组")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ValueError(f"第 {i} 条不是对象")
        if "template_key" in r and "profile_key" not in r:
            raise ValueError(f"第 {i} 条用了作废字段名 template_key(R6-60 (b):以 02 §3.1 DDL 的 profile_key 为准)")
        key = str(r.get("profile_key") or "").strip()
        if not key:
            raise ValueError(f"第 {i} 条缺 profile_key")
        if key in seen:
            raise ValueError(f"profile_key 重复:{key}")
        seen.add(key)
        t = dict(r)
        t["profile_key"] = key
        t.setdefault("weight", DEFAULT_WEIGHT)
        out.append(t)
    if not out:
        raise ValueError("templates 为空")
    return out


class Library:
    """一次加载、常驻内存的档案库视图(库文件是只读的,进程内不重读)。"""

    def __init__(self, path: Optional[str] = None, *, read_text: Optional[Callable[[str], Optional[str]]] = None):
        self.path = path
        self.error: Optional[str] = None
        reader = read_text or _read_text
        raw = reader(path) if path else None
        if raw is None:
            if path:
                self.error = "库文件读不到"
                log.error("机型档案库读不到(%s):回落到内置 %d 条保底清单 —— 新建企点账号的机型池会变小,"
                          "请确认 [device_profiles] library 与随包落点一致", path, len(BUILTIN_FALLBACK))
            self._rows = [dict(t) for t in BUILTIN_FALLBACK]
            self.source = SOURCE_BUILTIN
            return
        try:
            self._rows = parse_library(raw)
            self.source = SOURCE_LIBRARY
        except (ValueError, TypeError) as e:
            self.error = str(e)
            log.error("机型档案库 %s 解析失败(%s):回落到内置 %d 条保底清单", path, e, len(BUILTIN_FALLBACK))
            self._rows = [dict(t) for t in BUILTIN_FALLBACK]
            self.source = SOURCE_BUILTIN

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def all(self) -> list[dict[str, Any]]:
        """完整模板(分配方用)。"""
        return [dict(t) for t in self._rows]

    def list_view(self) -> list[dict[str, Any]]:
        """``#24`` 的列表视图:只回 02 §3.4 #24 定死的五个键。"""
        return [{k: t.get(k, DEFAULT_WEIGHT if k == "weight" else None) for k in LIST_KEYS} for t in self._rows]

    def by_key(self, profile_key: str) -> Optional[dict[str, Any]]:
        """按 ``profile_key`` 取完整模板(分配方用);不存在回 ``None``。"""
        return next((dict(t) for t in self._rows if t["profile_key"] == profile_key), None)


def load(cfg=None, *, path: Optional[str] = None, read_text: Optional[Callable[[str], Optional[str]]] = None) -> Library:
    """按 ``[device_profiles] library`` 建库视图。``cfg`` 给 ``AgentConfig``,或直接给 ``path``。"""
    if path is None and cfg is not None:
        path = getattr(getattr(cfg, "device_profiles", None), "library", None)
    return Library(path, read_text=read_text)


#: 缺省视图(没装配 / 没给配置时用):只有内置回落清单,**不读盘**
def builtin() -> Library:
    return Library(None)


def templates(lib: Optional[Library] = None) -> list[dict[str, Any]]:
    """#24 的列表视图;``lib`` 缺省 = 内置回落清单(装配好的调用方应显式传 ``agent.device_profiles``)。"""
    return (lib or builtin()).list_view()


def by_key(profile_key: str, lib: Optional[Library] = None) -> Optional[dict[str, Any]]:
    return (lib or builtin()).by_key(profile_key)


def library_path_default() -> str:
    """05 §7 的字面默认值(供日志/自检打印;真值仍走 ``cfg.device_profiles.library``)。"""
    return os.path.join("/opt/qtrade/agent/data", "device_profiles.json")
