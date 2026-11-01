#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字核查：agent 说的数字，必须在知识库里能找到依据。

知识库是组员采集的「事实卡」导出文件（knowledge/知识库.json）。
1.8 起优先用随包附带的 knowledge/，没有再回落到兄弟目录的 knowledge/。
知识库为空时自动跳过，不误报。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_KB_DIR = _HERE / "knowledge"
if not _KB_DIR.is_dir():
    _KB_DIR = _HERE.parent / "knowledge"
KB_PATH = _KB_DIR / "知识库.json"

# 这些数字太常见，不当作"需要举证的声称"
SAFE_YEARS = {str(y) for y in range(2024, 2028)}
SAFE_SMALL = {str(n) for n in range(1, 10)}

# 数字 + 单位 才算"声称"（避免把"第 3 轮""2 个方案"这种也报出来）
TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|个百分点|万亿|亿|万|项|家|个|倍|台|张|"
    r"EFLOPS|PFLOPS|TFLOPS|TOPS|GW|MW|亿美元|美元|元|轮|年)"
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _norm(s: str) -> str:
    """去掉所有空白和全角空格，方便比对。"""
    return re.sub(r"[\s\u3000]+", "", s or "")


def kb_from(raw: dict) -> dict:
    """把任意知识库结构（旧的 us/cn 或新的 common/parties）压成一段可比对文本。"""
    if not isinstance(raw, dict) or not raw:
        return {"_empty": True, "text": ""}

    parts: list[str] = []

    def harvest(v):
        if isinstance(v, str):
            if v.strip():
                parts.append(v)
        elif isinstance(v, list):
            for item in v:
                harvest(item)
        elif isinstance(v, dict):
            for item in v.values():
                harvest(item)

    for key, block in raw.items():
        if str(key).startswith("_"):
            continue
        harvest(block)
    text = _norm(" ".join(parts))
    return {"_empty": not text, "text": text}


def load_kb(topic_id: str | None = None) -> dict:
    """按议题读库；没有就回落到原来的单一库。"""
    if topic_id:
        p = KB_PATH.parent / "topics" / (str(topic_id).strip().lower() + ".json")
        if p.exists():
            try:
                return kb_from(json.loads(p.read_text(encoding="utf-8-sig")))
            except Exception:
                pass
    if not KB_PATH.exists():
        return {"_empty": True, "text": ""}
    try:
        return kb_from(json.loads(KB_PATH.read_text(encoding="utf-8-sig")))
    except Exception:
        return {"_empty": True, "text": ""}


def extract_claims(text: str) -> list[str]:
    """抽出需要举证的片段（数字+单位、年份）。"""
    claims = []
    for m in TOKEN_RE.finditer(text or ""):
        claims.append(_norm(m.group(0)))
    for m in YEAR_RE.finditer(text or ""):
        claims.append(m.group(1))
    # 去重保序
    return list(dict.fromkeys(claims))


def check(text: str, kb: dict | None = None) -> dict:
    """返回 {ok, skipped, flagged:[{token, reason}], checked:n}"""
    kb = kb if kb is not None else load_kb()
    if kb.get("_empty"):
        return {"ok": True, "skipped": True, "flagged": [], "checked": 0,
                "note": "知识库为空，本次跳过核查"}

    kb_text = kb["text"]
    flagged = []
    claims = extract_claims(text)
    for token in claims:
        digits = re.match(r"\d+(?:\.\d+)?", token)
        d = digits.group(0) if digits else token

        if token in kb_text:
            continue
        # 年份和 1-9 的小数字不要求举证
        if token in SAFE_YEARS or (d in SAFE_SMALL and not token.endswith(("%", "万亿", "亿"))):
            continue
        # 三位以上的数字，只要出现过就认（口径不同但同源）
        if len(d) >= 3 and d in kb_text:
            continue
        flagged.append({"token": token,
                        "reason": "知识库中找不到该数字的依据"})

    return {"ok": not flagged, "skipped": False, "flagged": flagged,
            "checked": len(claims)}
