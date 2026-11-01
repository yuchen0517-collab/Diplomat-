#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外交谈判 Demo 1.9 · 本地后端（局域网可访问）

启动：
    python server.py  →  浏览器打开 http://127.0.0.1:8001

页面（webdemo1.9）：
    /                      首页（主题版）
    /learn/                学习模式（三关地图 → 课程 → 学习笔记）
    /select/start.html     起手式（双边 / 多边 / 观战）
    /negotiate.html        谈判页（真实 Agent）
    /_fin2.html            观战单页（强制 UNCLOS 九方，双击即可演示）
    /report/loading.html   复盘生成中 → /report/index.html

1.9 变化：
    - 双边谈判页左侧换成战情解说，保留原对话区与右侧监控面板
    - 双边启用事件历史与实时谈判树，不改变后端谈判流程

1.8 已有基础：
    - 观战谈判页换成新版 UI：章节条 / 解说席 / 内心 OS / 实时思维导图
    - 内心 OS 按代表发言现场生成，并比对知识库事实卡
    - 思维导图不再写死，由每条发言实时长出来
    - 新增 /api/kb：把议题知识库摘要（阶段 / 各方立场）交给前端
    - knowledge/ 与 demo1.1/ 随包附带，整个目录可直接拷走运行
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sys
import threading
import time
import types
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
SITE = BASE / "site"
HOME_PAGE = os.environ.get("DEMO_HOME_PAGE", "index.html")

_ENGINE_DIR = Path(os.environ.get("DEMO_ENGINE_DIR", str(BASE / "demo1.1"))).resolve()
if not (_ENGINE_DIR / "neg_cli.py").exists():
    _ENGINE_DIR = BASE.parent / "demo1.1"
sys.path.insert(0, str(_ENGINE_DIR))
import neg_cli as engine  # noqa: E402
import factcheck  # noqa: E402

# ---------------- HiAgent 大脑（可选） ----------------
# DEMO_BRAIN=hiagent 或 hiagent/config.json 配好 base_url + api_key 时，
# neg_cli 里所有模型调用都会改走 HiAgent 工作流；否则保持原来的 DeepSeek 直连。
try:
    from hiagent.brain import install as _install_hiagent, status_of as _brain_status
    HIAGENT_BRAIN = _install_hiagent(engine)
except Exception as _brain_exc:  # 配置写错也要能启动，但要把话说清楚
    _brain_error = str(_brain_exc)
    print(f"  [大脑] HiAgent 未启用：{_brain_error}")
    HIAGENT_BRAIN = None

    def _brain_status(_engine):
        return {"provider": "deepseek", "enabled": False, "last_error": _brain_error}

HOST = os.environ.get("DEMO_HOST", "0.0.0.0")
PORT = int(os.environ.get("DEMO_PORT", "8001"))
MAX_ROUNDS = int(os.environ.get("DEMO_MAX_ROUNDS", "30"))
# 观战是「看完整场历史谈判」，默认不设上限（0 = 不限）
OBSERVE_ROUNDS = int(os.environ.get("DEMO_OBSERVE_ROUNDS", "0"))
SESSION_TTL = 3600
# 设置后所有浏览器共用同一局（「直播」观看用）：DEMO_SESSION_ID=live
FIXED_SID = os.environ.get("DEMO_SESSION_ID", "").strip()
# 每轮结束后是否调用模型判定隐藏任务（默认开；演示省钱可设 0）
TASK_JUDGE = os.environ.get("DEMO_TASK_JUDGE", "1").strip() not in ("0", "off", "false", "no")
# 单条发言字数上限（含标点）：观战一场几十轮，发言过长会把对话流和树撑爆
SPEECH_LIMIT = int(os.environ.get("DEMO_SPEECH_LIMIT", "350"))
SHIM = types.SimpleNamespace(no_json_mode=False, temperature=0.7)

AGENTS = engine.load_agents()
SCENARIO = AGENTS.get("_scenario", {})
CFG = AGENTS["us"]
MD = engine.load_prompt_markdown()

# ---------------- 场景：选择页选定的「你 + 对手 + 议题」 ----------------
PARTY = {
    "cn": "中国", "us": "美国", "eu": "欧盟", "in": "印度", "br": "巴西",
    "jp": "日本", "ru": "俄罗斯", "uk": "英国", "kr": "韩国",
    "tr": "土耳其", "id": "印度尼西亚", "eg": "埃及",
    "su": "苏联", "sg": "新加坡", "za": "南非", "sa": "沙特", "mv": "马尔代夫",
    "fr": "法国", "de": "德国", "it": "意大利", "ie": "爱尔兰",
    "au": "澳大利亚", "nz": "新西兰", "vn": "越南", "th": "泰国",
}
FLAG = {
    "cn": "🇨🇳", "us": "🇺🇸", "eu": "🇪🇺", "in": "🇮🇳", "br": "🇧🇷",
    "jp": "🇯🇵", "ru": "🇷🇺", "uk": "🇬🇧", "kr": "🇰🇷",
    "tr": "🇹🇷", "id": "🇮🇩", "eg": "🇪🇬",
    "su": "🇸🇺", "sg": "🇸🇬", "za": "🇿🇦", "sa": "🇸🇦", "mv": "🇲🇻",
    "fr": "🇫🇷", "de": "🇩🇪", "it": "🇮🇹", "ie": "🇮🇪",
    "au": "🇦🇺", "nz": "🇳🇿", "vn": "🇻🇳", "th": "🇹🇭",
}
DEFAULT_SCENE = {"agent": "us", "player": "cn", "rivals": ["us"],
                 "topic": "", "topic_id": "", "mode": "duel"}
GENERIC_TASKS = [
    {"text": "在核心议题上守住本方底线", "priority": "高", "done": False},
    {"text": "争取对方在程序性安排上先让一步", "priority": "中", "done": False},
    {"text": "避免被拖入对方设定的议题框架", "priority": "高", "done": False},
    {"text": "在收尾文本中保留本方的可执行承诺", "priority": "中", "done": False},
]


def agent_cfg_for(key: str) -> dict:
    """优先用 agents.json 里的完整人设；没有就用名称生成一个通用人设。"""
    cfg = AGENTS.get(key)
    if cfg:
        return cfg
    name = PARTY.get(key, key)
    return {
        "label": name,
        "agent_identity": name + "谈判代表",
        "agent_entity": name,
        "agent_position": "特命全权代表",
        "negotiation_style": "务实、注重实际利益交换，表态谨慎但有明确底线。",
        "public_position": "维护本国核心利益，主张通过对话解决分歧，反对单边施压。",
        "core_interests_ranked": "1. 国家安全 2. 经济发展空间 3. 国际话语权 4. 区域影响力",
        "red_lines": "1. 不接受损害本国主权的安排 2. 不接受不平等条款 3. 不接受被迫选边",
        "batna": "若谈判无果，将维持现状并通过其他多边渠道推进本方主张。",
        "hidden_task_card": "争取在最终文本中写入有利于本方的程序性条款。",
        "initial_interest": 50, "initial_image": 68, "initial_trust": 62,
        "strategy": {"template": "tit_for_tat", "baseline": "escalate",
                     "trust_threshold": 60,
                     "escalate_triggers": ["歧视性", "单边制裁", "干涉内政"],
                     "cooperate_conditions": ["对等", "互认", "具体数字"]},
    }


AI_KEYWORDS = ("AI", "人工智能", "算力", "芯片", "半导体", "模型", "智能",
               "数据", "算法", "技术标准", "出口管制")


def is_ai_topic(topic: str) -> bool:
    return any(k in (topic or "") for k in AI_KEYWORDS)


def normalize_scene(scene: dict | None) -> dict:
    """把起手式写进 localStorage 的 neg-scene 归一化成后端好用的形状。

    兼容：
      - 双边 {version:'1v1', you, rivals:[...]}
      - 多边 {version:'1vn', you, rivals:[...]}
      - 观战 {version:'observe', you:null, rivals:[...]}
    """
    sc = dict(DEFAULT_SCENE)
    src = scene if isinstance(scene, dict) else {}
    if src:
        for k, v in src.items():
            if v not in (None, "", []):
                sc[k] = v
    # 前端写的是 topicId（驼峰），后端统一用 topic_id
    if not sc.get("topic_id") and src.get("topicId"):
        sc["topic_id"] = str(src["topicId"])

    rivals = sc.get("rivals")
    if not isinstance(rivals, list) or not rivals:
        rivals = [sc.get("agent") or "us"]
    rivals = [str(r) for r in rivals if isinstance(r, str) and r][:16]   # UNCLOS 观战有 9 方
    if not rivals:
        rivals = ["us"]
    sc["rivals"] = rivals

    # 注意：DEFAULT_SCENE 自带 mode=duel，不能拿它判断来意；要看调用方原始传入的 mode/version
    raw_mode = str(src.get("mode") or src.get("version") or "").lower()
    if raw_mode in ("observe", "watch", "observer"):
        mode = "observe"
    elif raw_mode in ("multi", "1vn", "1v n"):
        mode = "multi"
    elif raw_mode in ("duel", "1v1"):
        mode = "duel"
    else:
        mode = "multi" if len(rivals) > 1 else "duel"
    sc["mode"] = mode

    player = sc.get("player") or sc.get("you") or ("cn")
    sc["player"] = player
    sc["agent"] = sc.get("agent") if sc.get("agent") in rivals else rivals[0]
    return sc


def scene_ctx(scene: dict | None, speaker: str | None = None) -> dict:
    sc = normalize_scene(scene)
    ak = speaker if speaker in sc["rivals"] else (sc.get("agent") or sc["rivals"][0])
    pk = sc.get("player") or "cn"
    cfg = agent_cfg_for(ak)
    topic = (sc.get("topic") or "").strip() or (SCENARIO.get("agenda") or ["谈判议题"])[0]

    scenario = dict(SCENARIO)
    scenario["name"] = topic
    scenario["agenda"] = [topic]
    ai_related = is_ai_topic(topic)
    if not ai_related:
        # 不要让它自称「联合国AI治理特别会议」——会和议题打架
        scenario["venue"] = ("多边磋商会议" if sc["mode"] == "multi"
                             else "观察席" if sc["mode"] == "observe"
                             else "双边磋商")
        # 人设也要换成议题中立版，否则「维护 AI 技术霸权」会出现在渔业/农产品谈判里
        label = PARTY.get(ak, cfg.get("label", ak))
        cfg = dict(cfg)
        cfg["agent_identity"] = label + "谈判代表团团长"
        cfg["agent_entity"] = label
        cfg["agent_position"] = "特命全权谈判代表"
        cfg["negotiation_style"] = (
            "务实强硬，先立原则再谈条件；习惯用程序性安排换取实质利益，"
            "在核心关切上不轻易松口，但会给对方留可执行的下台阶。")
        cfg["public_position"] = (
            "在「" + topic + "」议题上维护" + label + "的核心利益，主张以规则、对等和"
            "可核查的安排解决分歧，反对单方面施压与歧视性条款。")
        cfg["core_interests_ranked"] = (
            "1. 核心主权与安全关切 2. 现实经济利益 3. 地区与多边影响力 4. 履约的可执行性")
        cfg["red_lines"] = (
            "1. 不接受损害本方核心利益的安排 2. 不接受不平等或歧视性条款 "
            "3. 不接受被迫在本方核心关切上单方面让步")
        cfg["batna"] = (
            "若谈判无果，将维持现状，并通过其他双边或多边渠道继续推进本方主张。")
        cfg["hidden_task_card"] = (
            "争取在最终文本中写入有利于本方的程序性条款，同时避免被对方绑定额外义务。")
    player = dict(SCENARIO.get("player") or {})
    player["label"] = PARTY.get(pk, pk)
    player["full"] = PARTY.get(pk, pk)
    player["role"] = PARTY.get(pk, pk) + "谈判代表"
    if pk != "cn" or not ai_related:
        # 非中国席位 / 非 AI 议题：换成议题中立的通用任务，避免「AI 安全联盟」乱入
        player["hidden_tasks"] = GENERIC_TASKS
    if sc["mode"] == "observe":
        # 观战是多边会场：没有单一「对方」，发言面向全体
        player = dict(player)
        player["label"] = "在座各方"
        player["full"] = "在座各方"
        player["role"] = "共 " + str(len(sc["rivals"])) + " 方代表团"
    scenario["player"] = player

    strat = cfg.get("strategy") or {}
    return {
        "scene_raw": sc,
        "agent_key": ak, "player_key": pk,
        "rivals": sc["rivals"], "mode": sc["mode"],
        "agent_cfg": cfg, "player_cfg": player,
        "agent_label": PARTY.get(ak, cfg.get("label", ak)),
        "player_label": PARTY.get(pk, pk),
        "agent_flag": FLAG.get(ak, "🏳"),
        "player_flag": FLAG.get(pk, "🏳"),
        "scenario": scenario,
        "topic": topic,
        "ai_related": ai_related,
        "strategy": strat,
        "trust_threshold": int(strat.get("trust_threshold", 60)),
        "triggers": strat.get("escalate_triggers") or [],
        "conditions": strat.get("cooperate_conditions") or [],
    }


OBSERVE_NOTE = (
    "\n\n## 会场提示（多边观战）\n"
    "这是多边会场，你不是在和某一个对手双边谈判。\n"
    "- 发言面向在座各方，开场用「主席先生、各位代表」这类多边称呼。\n"
    "- 可以点名回应刚才发言的那一方（例如「关于苏联代表提到的领海宽度」），"
    "但不要把所有人当成同一个对象。\n"
    "- 立场要能同时被不同集团听懂：77 国集团关心管辖范围，海运国关心通行自由，"
    "地理不利国关心资源分配。\n"
)


def system_prompt_now(scene: dict | None = None, speaker: str | None = None) -> str:
    """每轮重新构建：更新事实卡后不用重启服务。

    非 AI 类议题不注入 AI 事实卡，否则会出现「拿芯片数据谈农产品」的错位。
    """
    ctx = scene_ctx(scene, speaker=speaker)
    sc = ctx["scene_raw"]
    tid = (sc.get("topic_id") or "").strip()
    kb = engine.load_knowledge(tid) if tid else {}
    if not kb and ctx["ai_related"]:
        tid = "compute-chip"          # AI 议题没写 topicId 时兜底
        kb = engine.load_knowledge(tid)
    side = None
    include_all = ctx["mode"] in ("observe", "multi")
    if kb:
        side = ctx["agent_key"]
    text = engine.build_system_prompt(MD, ctx["agent_cfg"], ctx["scenario"],
                                      side=side, topic_id=tid or None,
                                      include_all=include_all)
    if ctx["mode"] == "observe":
        text += OBSERVE_NOTE
    text += ("\n\n## 会场事件（硬性）\n"
             "每轮输入可能带【本轮突发事件】或【会场近期事件】。\n"
             "- 出现突发事件时，response 必须至少用一句话回应：点名事件或受影响方，"
             "并说明它如何改变本方的判断、要价或条件；不要只复述事件标题。\n"
             "- 标注为「会场花絮」的事件不改变任何指数，可以一句带过，不要强行把它说成谈判筹码。\n"
             "- 近期事件仍在生效，不要表现得像没发生过；可以引用、追责或借势。\n")
    text += ("\n\n## 发言长度（硬性）\n"
             f"response 必须控制在 {SPEECH_LIMIT} 个汉字以内（含标点）。"
             "把立场、条件、要求说清即可，不要铺陈背景；超长部分会被系统截断。\n")
    return text


# 默认值（用于启动横幅和兜底）
_D = scene_ctx(DEFAULT_SCENE)
AGENT_LABEL = _D["agent_label"]
PLAYER_CFG = _D["player_cfg"]
PLAYER_LABEL = _D["player_label"]
TOPIC = _D["topic"]

# ---------------- 博弈层 ----------------
GAMES = SCENARIO.get("games") or {}
GAME_SWITCH = int(GAMES.get("switch_round", 5))
US_STRAT = CFG.get("strategy") or {}
TRUST_THRESHOLD = int(US_STRAT.get("trust_threshold", 60))
TRIGGERS = US_STRAT.get("escalate_triggers") or []
CONDITIONS = US_STRAT.get("cooperate_conditions") or []
DEFAULT_GAME = {"name": "囚徒困境", "topic": "",
                "payoff": {"CC": [0, 0], "CD": [0, 0], "DC": [0, 0], "DD": [0, 0]}}


# 博弈结构按轮次切换，但「博弈议题」必须跟着玩家的谈判议题走
PHASE_LABEL = {
    "prisoner_dilemma": "交换与反制",
    "stag_hunt": "规则协调与共享",
}


def game_for(round_no: int):
    """前几轮打囚徒困境（交换与反制），后面换猎鹿（协调与共享）。"""
    key = "prisoner_dilemma" if round_no < GAME_SWITCH else "stag_hunt"
    return key, (GAMES.get(key) or DEFAULT_GAME)


def game_topic_for(topic: str, gkey: str, g: dict) -> str:
    """谈判议题 + 阶段，例如「半导体设备出口许可的个案豁免 · 交换与反制」。"""
    phase = PHASE_LABEL.get(gkey) or g.get("topic", "")
    topic = (topic or "").strip()
    if not topic:
        return phase
    return topic + " · " + phase if phase else topic


def trigger_hit(text: str, triggers=None):
    for k in (triggers if triggers is not None else TRIGGERS):
        if k in text:
            return k
    return None


def has_concrete(text: str, conditions=None) -> bool:
    """是否给出具体对价：出现数字，或出现合作条件关键词。"""
    if any(ch.isdigit() for ch in text):
        return True
    return any(k in text for k in (conditions if conditions is not None else CONDITIONS))


def norm_move(v) -> str:
    v = str(v or "").strip().lower()
    if v.startswith("esc"):
        return "escalate"
    if v.startswith("coop"):
        return "cooperate"
    return "ambiguous"


def payoff_pair(value) -> tuple[int, int]:
    """agents.json 里的收益可能写成 "3 3" 或 [3, 3]，统一解析成两个整数。"""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return 0, 0
    if isinstance(value, str):
        nums = re.findall(r"-?\d+", value)
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
    return 0, 0


def resolve_game(st, obj, round_no, user_text, prev_a, prev_p, ctx=None):
    """三层判定：矩阵 → 角色模板 → 知识卡硬约束。返回本轮的记录与结算后数值。"""
    ctx = ctx or scene_ctx(st.get("scene"))
    gkey, g = game_for(round_no)
    gm = obj.get("game") or {}
    pm = norm_move(gm.get("player_move"))
    am = norm_move(gm.get("agent_strategy"))
    if am == "ambiguous":
        am = "escalate"          # 模型没给明确姿态时，按角色基线（强硬）走

    notes = []
    hit = trigger_hit(user_text, ctx["triggers"])
    if hit:
        if am != "escalate":
            notes.append("触发红线「" + hit + "」，强制转为强硬")
        else:
            notes.append("触发红线「" + hit + "」")
        am = "escalate"
    elif am == "cooperate":
        blockers = []
        if prev_p["trust"] < ctx["trust_threshold"]:
            blockers.append("信任度 " + str(prev_p["trust"]) + " 低于阈值 " + str(ctx["trust_threshold"]))
        if not has_concrete(user_text, ctx["conditions"]):
            blockers.append("未给出具体对价")
        if blockers:
            am = "escalate"
            notes.append("合作窗口未开启：" + "；".join(blockers))
        else:
            notes.append("合作窗口开启：给出具体对价且未触红线")

    if not notes:
        notes.append("按角色模板出牌（以牙还牙 · 基线强硬）")

    pk = "C" if pm == "cooperate" else "D"
    ak = "C" if am == "cooperate" else "D"
    p_gain, a_gain = payoff_pair(g.get("payoff", {}).get(pk + ak))

    record = {
        "round": round_no,
        "game": g.get("name", gkey),
        "game_topic": game_topic_for(ctx["topic"], gkey, g),
        "matrix": pk + ak,
        "player_move": pm,
        "agent_move": am,
        "payoff": {"player": p_gain, "agent": a_gain},
        "attribution": "；".join(notes),
    }
    return {
        "record": record,
        "player_interest": max(0, min(100, prev_p["interest"] + p_gain)),
        "agent_interest": max(0, min(100, prev_a["interest"] + a_gain)),
    }

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


EVENTS = (SCENARIO.get("events") or [])

# 议题事件池：knowledge/event_cards.json
# 键 = 知识库 topic id（compute-chip / unclos），neutral 为两会通用的「无影响」花絮。
EVENT_CARDS_FILE = BASE / "knowledge" / "event_cards.json"


def load_event_cards() -> dict:
    try:
        raw = json.loads(EVENT_CARDS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, list)}


EVENT_CARDS = load_event_cards()

# 非 AI 议题用中立事件池，避免「AGI 突破」出现在渔业 / 农产品谈判里
#
# observe 字段只在九方观战里生效：事件不再默认算在「中国 + 当前发言方」头上。
#   targets: all（全体）/ one（随机一方）/ ["cn","us"]（指定几方）
#   effects: 该目标方的三项指数增减（不带 _delta 后缀）
#   others : 其余各方的附带影响（可选）
GENERIC_EVENTS = [
    {"id": "G01", "title": "临时休会",
     "text": "一方代表团要求休会内部磋商，会场节奏被打断",
     "observe": {"targets": "one", "role": "请求休会方",
                 "effects": {"interest_delta": 2, "trust_delta": -1},
                 "others": {"effects": {"trust_delta": -1}}},
     "effects": {"player": {"interest_delta": 2}, "agent": {"interest_delta": 2, "trust_delta": -2}}},
    {"id": "G02", "title": "会谈文本外泄",
     "text": "部分条款草案被媒体提前披露，各方表态趋于谨慎",
     "observe": {"targets": "all", "effects": {"image_delta": -3, "trust_delta": -2}},
     "effects": {"player": {"image_delta": -4}, "agent": {"image_delta": -3, "trust_delta": -3}}},
    {"id": "G03", "title": "第三方表态",
     "text": "有第三方公开呼吁各方保持克制，为会谈创造窗口",
     "observe": {"targets": "all", "effects": {"image_delta": 2, "trust_delta": 3}},
     "effects": {"player": {"image_delta": 3, "trust_delta": 4},
                 "agent": {"image_delta": 2, "trust_delta": 2}}},
    {"id": "G04", "title": "国内压力上升",
     "text": "一方国内舆论要求代表团提高要价，谈判空间收窄",
     "observe": {"targets": "one", "role": "国内压力方",
                 "effects": {"interest_delta": 3, "trust_delta": -3}},
     "effects": {"player": {"interest_delta": 3, "trust_delta": -3},
                 "agent": {"interest_delta": 3, "trust_delta": -4}}},
    {"id": "G05", "title": "程序性突破",
     "text": "各方就先易后难的讨论顺序达成初步一致",
     "observe": {"targets": "all", "effects": {"interest_delta": 1, "image_delta": 3}},
     "effects": {"player": {"interest_delta": 2, "image_delta": 3},
                 "agent": {"interest_delta": 1, "image_delta": 3}}},
    {"id": "G06", "title": "会场抗议",
     "text": "会场外出现抗议活动，各方需要回应公众关切",
     "observe": {"targets": "all", "effects": {"image_delta": 2, "trust_delta": -2}},
     "effects": {"player": {"image_delta": 2, "trust_delta": -2},
                 "agent": {"image_delta": 1, "trust_delta": -2}}},
]

# agents.json 里的 AI 议题事件是「中国 vs 某对手」写法的，观战九方场按 id 换一套归因；
# 不直接改 agents.json，避免影响 demo1.1 的命令行版本。
OBSERVE_EVENT_RULES = {
    "D01": {"targets": "one", "role": "被曝方",
            "effects": {"image_delta": -8, "trust_delta": -6},
            "others": {"effects": {"image_delta": 2}}},
    "D02": {"targets": "all", "effects": {"interest_delta": 2, "image_delta": 2}},
    "D03": {"targets": "all", "effects": {"interest_delta": -2, "image_delta": -1}},
    "D04": {"targets": "one", "role": "制裁发起方",
            "effects": {"interest_delta": 4, "image_delta": -4},
            "others": {"effects": {"interest_delta": -2, "trust_delta": -2}}},
    "D05": {"targets": "all", "effects": {"image_delta": 3, "trust_delta": 2}},
    "D06": {"targets": "one", "role": "当事国",
            "effects": {"image_delta": -4, "trust_delta": -3},
            "others": {"effects": {"image_delta": 1}}},
    "D07": {"targets": "all", "effects": {"interest_delta": -1, "image_delta": -1}},
    "D08": {"targets": "one", "role": "突破方",
            "effects": {"interest_delta": 6, "image_delta": 3},
            "others": {"effects": {"interest_delta": -2}}},
}
EVENT_PROB = float(os.environ.get("DEMO_EVENT_PROB", "0.2"))
# 0 = 不限次数；原先默认 3，长局到后面就再也不出事件
MAX_EVENTS = int(os.environ.get("DEMO_MAX_EVENTS", "0"))
# 两次事件之间至少隔几轮，避免连续弹卡打断观看
EVENT_MIN_GAP = max(0, int(os.environ.get("DEMO_EVENT_MIN_GAP", "3")))
# 连续这么多轮没事件就强制来一个，避免长局中间长时间冷场（0 = 不强制）
EVENT_GAP = max(0, int(os.environ.get("DEMO_EVENT_GAP", "6")))


def event_topic_key(st: dict) -> str:
    """把起手式的 topicId 映射到事件池键；没有专属池就回落到旧事件池。"""
    raw = str((st.get("scene") or {}).get("topic_id") or "").strip().lower()
    alias = getattr(engine, "TOPIC_ALIAS", {}) or {}
    tid = alias.get(raw, raw)
    if tid in EVENT_CARDS:
        return tid
    topic = str(st.get("topic") or "")
    if "海洋法" in topic or "公海" in topic or "unclos" in topic.lower():
        return "unclos"
    if is_ai_topic(topic):
        return "compute-chip"
    return ""


def events_for(st: dict) -> list[dict]:
    neutral = list(EVENT_CARDS.get("neutral") or [])
    key = event_topic_key(st)
    if key:
        pool = list(EVENT_CARDS.get(key) or [])
        if pool:
            return pool + neutral
    return (EVENTS if is_ai_topic(st.get("topic", "")) else GENERIC_EVENTS) + neutral


def maybe_event(st: dict):
    """随机抽事件：一轮内先洗牌抽完，再重新洗牌，允许跨轮重复。"""
    pool_all = events_for(st)
    if not pool_all:
        return None
    if MAX_EVENTS > 0 and len(st["events"]) >= MAX_EVENTS:
        return None
    if st["round"] < 1:
        return None
    if EVENT_PROB <= 0:
        return None

    last_round = max((int(e.get("round") or 0) for e in st["events"]), default=0)
    # 最短间隔：刚弹过卡的几轮内不再抽，避免连续打断观看
    if EVENT_MIN_GAP > 0 and st["events"] and (st["round"] - last_round) < EVENT_MIN_GAP:
        return None

    pool_ids = [e.get("id") for e in pool_all if e.get("id")]
    remaining = [eid for eid in (st.get("event_bag") or []) if eid in pool_ids]
    if not remaining:
        remaining = list(pool_ids)
        random.shuffle(remaining)
        # 新一轮洗牌时，避免第一张和上一轮最后一张撞车
        last_id = st["events"][-1].get("id") if st["events"] else None
        if len(remaining) > 1 and remaining[-1] == last_id:
            remaining[0], remaining[-1] = remaining[-1], remaining[0]

    # 概率没抽中时，如果已经连续好几轮没事件，就强制补一个，避免长局中段彻底冷场
    force = False
    if EVENT_GAP > 0:
        force = (st["round"] - last_round) >= EVENT_GAP
    if not force and random.random() > EVENT_PROB:
        return None

    pick_id = remaining.pop()
    st["event_bag"] = remaining
    for ev in pool_all:
        if ev.get("id") == pick_id:
            return ev
    return None


OBSERVE_FIELDS = ("interest", "image", "trust")


def event_observe_rule(ev: dict) -> dict:
    rule = ev.get("observe")
    if isinstance(rule, dict):
        return rule
    return OBSERVE_EVENT_RULES.get(str(ev.get("id") or ""), {})


def _observe_deltas(raw) -> dict:
    out = {}
    for field in OBSERVE_FIELDS:
        try:
            value = int((raw or {}).get(field + "_delta", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            out[field] = value
    return out


INDEX_LABEL = {"interest": "利益", "image": "形象", "trust": "信任"}


def _deltas_text(deltas: dict) -> str:
    chunks = []
    for field in OBSERVE_FIELDS:
        value = int((deltas or {}).get(field, 0) or 0)
        if value:
            chunks.append(INDEX_LABEL[field] + ("+" if value > 0 else "") + str(value))
    return "、".join(chunks)


def event_impact_text(impacts: list[dict]) -> str:
    """把 impacts 压成模型提示词里能直接读的一行，如「苏联（被曝方）形象-8」。"""
    parts = []
    for imp in impacts or []:
        text = _deltas_text(imp.get("deltas") or {})
        if not text:
            continue
        label = str(imp.get("label") or "各方")
        count = int(imp.get("count") or 1)
        if count > 1:
            label += "（" + str(count) + " 方）"
        elif imp.get("role"):
            label += "（" + str(imp["role"]) + "）"
        parts.append(label + " " + text)
    return "；".join(parts)


def legacy_event_effect_text(ev: dict, st: dict) -> str:
    """双边 / 多边沿用 player / agent 时的提示词影响描述。"""
    parts = []
    for side, label in (("player", st.get("player_label") or "我方"),
                        ("agent", st.get("agent_label") or "对方")):
        text = _deltas_text(_observe_deltas((ev.get("effects") or {}).get(side) or {}))
        if text:
            parts.append(str(label) + " " + text)
    return "；".join(parts)


def recent_event_lines(st: dict, limit: int = 3) -> list[str]:
    """最近几轮的事件，供后续发言继续引用；让事件不止影响发生的那一轮。"""
    lines = []
    for ev in (st.get("events") or [])[-max(1, limit):]:
        line = "第" + str(ev.get("round") or "?") + "轮《" + str(ev.get("title") or "事件") + "》" \
               + str(ev.get("text") or "").strip().rstrip("。；; ")
        if ev.get("no_effect"):
            line += "（会场花絮，无指数影响）"
        else:
            impact = event_impact_text(ev.get("impacts") or [])
            if impact:
                line += "（影响：" + impact + "）"
        lines.append(line)
    return lines


def resolve_event_impacts(st: dict, ev: dict, round_no: int | None = None) -> list[dict]:
    """把事件归因解析成「哪些代表团、各变多少」。

    观战没有「我方」，所以不能默认把影响算到中国 + 当前发言方头上。
    返回的 impacts 既是事件卡的展示数据，也是 apply_event 的结算依据。
    """
    rule = event_observe_rule(ev)
    rivals = [str(r) for r in (st.get("rivals") or []) if r]
    if not rule or not rivals:
        return []

    # 多组归因：一个事件可以同时命中「发起方 / 受限方 / 供应链方」等不同群体
    groups = rule.get("groups")
    if isinstance(groups, list) and groups:
        impacts, assigned = [], set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            gt = group.get("targets")
            if gt == "all":
                keys = [k for k in rivals if k not in assigned]
            else:
                keys = [str(k) for k in (gt or []) if str(k) in rivals and str(k) not in assigned]
            if not keys:
                continue
            deltas = _observe_deltas(group.get("effects"))
            if not deltas:
                continue
            if len(keys) == 1:
                label = group.get("label") or PARTY.get(keys[0], keys[0])
                role = group.get("role") or "当事方"
            else:
                label = group.get("label") or group.get("role") or "相关各方"
                role = group.get("role") or "相关各方"
            impacts.append({"keys": keys, "label": label, "role": role,
                            "count": len(keys), "deltas": deltas})
            assigned.update(keys)
        extra = rule.get("others") or {}
        rest = [k for k in rivals if k not in assigned]
        other_deltas = _observe_deltas(extra.get("effects"))
        if rest and other_deltas:
            impacts.append({"keys": rest,
                            "label": extra.get("label") or "其他各方",
                            "role": extra.get("role") or "其他各方",
                            "count": len(rest), "deltas": other_deltas})
        return impacts

    targets = rule.get("targets", "all")
    if targets == "none":
        return []
    if isinstance(targets, (list, tuple, set)):
        main = [str(k) for k in targets if str(k) in rivals]
        others = [k for k in rivals if k not in main]
    elif targets == "one":
        pool = [str(k) for k in (rule.get("pool") or rivals) if str(k) in rivals] or rivals
        # 用「事件 id + 轮次」做种子：同一局重绘不会换人
        seed_round = st.get("round") if round_no is None else round_no
        pick = random.Random(str(ev.get("id")) + "|" + str(seed_round)).choice(pool)
        main = [pick]
        others = [k for k in rivals if k != pick]
    else:
        main, others = rivals, []

    impacts = []
    deltas = _observe_deltas(rule.get("effects"))
    if main and deltas:
        if len(main) == 1:
            key = main[0]
            label = PARTY.get(key, key)
            role = rule.get("role") or "当事方"
        else:
            label = rule.get("label") or "全体代表团"
            role = rule.get("role") or "全体代表"
        impacts.append({"keys": main, "label": label, "role": role,
                        "count": len(main), "deltas": deltas})
    extra = rule.get("others") or {}
    other_deltas = _observe_deltas(extra.get("effects"))
    if others and other_deltas:
        impacts.append({"keys": others,
                        "label": extra.get("label") or "其他各方",
                        "role": extra.get("role") or "其他各方",
                        "count": len(others), "deltas": other_deltas})
    return impacts


def apply_event(st: dict, ev: dict, impacts: list[dict] | None = None) -> list[dict]:
    """事件是独立于模型之外的第二条指数修改通道。

    观战九方场：按 resolve_event_impacts() 结算到 party_indexes，并累加 party_delta，
    让事件卡上的数字和右侧指数板是同一笔账；不再默认修改中国。
    双边 / 多边：沿用 player / agent 两方结算，事件卡显示「我方 / 对方」。
    """
    if st.get("mode") == "observe":
        impacts = impacts if impacts is not None else resolve_event_impacts(st, ev)
        parties = st.setdefault("party_indexes", {})
        deltas_all = st.setdefault("party_delta", {})
        for imp in impacts:
            deltas = imp.get("deltas") or {}
            for key in imp.get("keys") or []:
                cur = parties.setdefault(key, {f: 50.0 for f in OBSERVE_FIELDS})
                row = deltas_all.setdefault(key, {f: 0.0 for f in OBSERVE_FIELDS})
                for field in OBSERVE_FIELDS:
                    delta = deltas.get(field, 0)
                    if not delta:
                        continue
                    before = float(cur.get(field, 50.0))
                    after = max(0.0, min(100.0, before + delta))
                    cur[field] = after
                    row[field] = round(float(row.get(field, 0.0)) + (after - before), 1)
        return impacts

    for field, side in (("player_indexes", "player"), ("agent_indexes", "agent")):
        eff = (ev.get("effects") or {}).get(side) or {}
        for k, dk in zip(engine.INDEX_KEYS, engine.DELTA_KEYS):
            st[field][k] = max(0, min(100, st[field][k] + int(eff.get(dk, 0))))
    return impacts or []


ADVISOR_PROMPT = (
    "你是本方首席谈判顾问，为谈判代表提供两句话的作战提示。\n"
    "根据给定的局势输出一个 JSON 对象：\n"
    '{"tip": "话术方向建议，具体可执行，不超过 60 字", '
    '"warn": "立场或风险提醒，不超过 60 字"}\n'
    "要求：结合双方指数差距和上一轮点评，不要空话套话；不要提与议题无关的技术领域。\n"
    "指数一律用中文名称（主体利益值、国际形象值、盟友信任度），不要出现 interest/image/trust。\n"
    "只输出 JSON，不要解释。"
)


def offline_advice(st: dict, ev) -> dict:
    """没有 API Key 时的本地建议，按局势给不同的话，不是死文案。"""
    p, a = st["player_indexes"], st["agent_indexes"]
    topic = st.get("topic") or "本议题"
    if ev:
        tip = "抓住「" + ev["title"] + "」的窗口，把它与「" + topic + "」的核心关切绑定。"
    elif p["interest"] < a["interest"]:
        tip = "我方利益值落后，建议用「有条件让步」换取对方在具体条款上的松动。"
    else:
        tip = "我方利益值占优，可提高要价，并要求对方把承诺写成可核查的条款。"
    if p["trust"] < 55:
        warn = "盟友信任度偏低，不要为短期利益牺牲长期伙伴关系。"
    elif p["image"] < 60:
        warn = "国际形象偏低，避免威胁式表态，改用规则与道义论据。"
    else:
        warn = "守住底线：核心利益不可作为交换筹码，让步必须换回对价。"
    return {"tip": tip, "warn": warn}


def call_advisor(st: dict, obj: dict, ev):
    """单独一个小请求请模型当军师，失败就降级到本地建议。"""
    key = current_key()
    if not key:
        return offline_advice(st, ev)
    os.environ["DEEPSEEK_API_KEY"] = key
    ss = obj.get("speaker_score") or {}
    ai_topic = is_ai_topic(st.get("topic", ""))
    ctx = {
        "round": st["round"],
        "max_rounds": st["max_rounds"],
        "议题": st.get("topic", ""),
        "我方": st.get("player_label", ""),
        "对手": st.get("agent_label", ""),
        "我方指数": st["player_indexes"],
        "对方指数": st["agent_indexes"],
        "对方上一轮发言": (obj.get("response") or "")[:400],
        "对方给我方评分": ss.get("score"),
        "点评": ss.get("feedback", ""),
        "本轮突发": (ev or {}).get("title", "无"),
    }
    if ai_topic:
        _tid = (st.get("scene") or {}).get("topic_id") or None
        ctx["我方可用数据"] = engine.key_numbers("cn", topic_id=_tid)
        ctx["对方可用数据"] = engine.key_numbers("us", topic_id=_tid)

    adv_sys = ADVISOR_PROMPT
    if st.get("mode") == "observe":
        # 观战没有「我方」：换成第三方观察视角的输入
        ctx = {
            "round": st["round"],
            "议题": st.get("topic", ""),
            "当前阶段": st.get("phase", ""),
            "发言方": st.get("speaker_label", ""),
            "发言方指数": st["agent_indexes"],
            "其他各方均值": st["player_indexes"],
            "本段发言": (obj.get("response") or "")[:400],
            "点评": ss.get("feedback", ""),
            "本轮突发": (ev or {}).get("title", "无"),
        }
        adv_sys = ADVISOR_PROMPT + (
            "\n\n【观战模式 · 发言解析】这是多边会场，没有「我方」，也没有「你」。"
            "你的任务不是出主意，而是**点评刚才那位代表的发言，让观察者学到东西**。\n"
            "- tip（立场与手法）：这段发言站在什么立场、依据了哪些事实或先例、"
            "用了什么手法（捆绑议题／以退为进／点名施压／争取集团背书）、在向谁喊话。\n"
            "- warn（观察看点）：谁被点名了、哪条分歧最可能在下一轮爆开、"
            "哪句话是可以验证真假的（数字、条款、承诺）。\n"
            "用「该代表」「发言方」指代当前发言人，用「各方」指代其他代表团；"
            "禁止出现「我方」「对方」「你需要」。各 60 字以内。")
    try:
        raw, _finish = engine.call_deepseek(
            [{"role": "system", "content": adv_sys},
             {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}],
            json_mode=True, temperature=0.6)
        o = engine.extract_json(raw)
        tip = str(o.get("tip", "")).strip()
        warn = str(o.get("warn", "")).strip()
        if tip or warn:
            return {"tip": tip, "warn": warn}
    except SystemExit:
        pass
    except Exception:
        pass
    return offline_advice(st, ev)


def current_key() -> str:
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    f = BASE / "key.txt"
    if f.exists():
        try:
            k = f.read_text(encoding="utf-8-sig").strip()
            if k:
                return k
        except Exception:
            pass
    # 只配了 HiAgent、没有 DeepSeek key 时，也要按「在线」处理，
    # 否则会走 OFFLINE_RESPONSES，根本不会调用 HiAgent。
    if getattr(engine, "HIAGENT_BRAIN", None) is not None:
        return "hiagent"
    return ""


def initial_state(scene=None) -> dict:
    ctx = scene_ctx(scene or DEFAULT_SCENE)
    pc = ctx["player_cfg"]
    return {
        "scene": ctx["scene_raw"],
        "mode": ctx["mode"],
        "rivals": ctx["rivals"],
        "you_key": ctx["player_key"],
        "speaker_key": ctx["agent_key"],
        "speaker_label": ctx["agent_label"],
        "topic": ctx["topic"],
        "player_label": ctx["player_label"],
        "agent_label": ctx["agent_label"],
        "player_flag": ctx["player_flag"],
        "agent_flag": ctx["agent_flag"],
        "party_indexes": {k: {"interest": 50, "image": 50, "trust": 50}
                          for k in ctx["rivals"]},
        "round": 0,
        "max_rounds": OBSERVE_ROUNDS if ctx["mode"] == "observe" else MAX_ROUNDS,
        "messages": [],
        "player_indexes": {k: int(pc.get(f"initial_{k}", 50)) for k in engine.INDEX_KEYS},
        "agent_indexes": {k: int(ctx["agent_cfg"].get(f"initial_{k}", 50)) for k in engine.INDEX_KEYS},
        "tasks": [
            {"text": t.get("text", ""), "priority": t.get("priority", ""), "done": bool(t.get("done"))}
            for t in (pc.get("hidden_tasks") or [])
        ],
        "last": None,
        "last_game": None,
        "game_log": [],
        "payoff_total": {"player": 0, "agent": 0},
        "assistant": None,
        "fact_stats": {"checked": 0, "flagged": 0},
        "events": [],
        "history": [],
        "started_at": None,      # 第一句话的时刻
        "pending": None,         # 正在等模型回应的轮次（前端显示「正在发言」）
        "finished": False,
        "elapsed": 0,            # 结束时冻结的总秒数
        "touched": time.time(),
    }


SESSIONS: dict[str, dict] = {}
SESS_LOCK = threading.Lock()


def sweep() -> None:
    cutoff = time.time() - SESSION_TTL
    for k in [k for k, v in SESSIONS.items() if v["touched"] < cutoff]:
        SESSIONS.pop(k, None)


def get_or_create(sid: str | None) -> tuple[str, dict, bool]:
    with SESS_LOCK:
        if FIXED_SID:
            st = SESSIONS.get(FIXED_SID)
            if st is None:
                SESSIONS[FIXED_SID] = initial_state(DEFAULT_SCENE)
                return FIXED_SID, SESSIONS[FIXED_SID], True
            st["touched"] = time.time()
            return FIXED_SID, st, sid != FIXED_SID
        if sid and sid in SESSIONS:
            SESSIONS[sid]["touched"] = time.time()
            return sid, SESSIONS[sid], False
        sweep()
        new_sid = sid if (sid and sid not in SESSIONS) else uuid.uuid4().hex
        SESSIONS[new_sid] = initial_state(DEFAULT_SCENE)
        return new_sid, SESSIONS[new_sid], True


def current_game_info(st: dict) -> dict:
    key, g = game_for(st["round"] + 1)
    return {
        "key": key,
        "name": g.get("name", ""),
        "phase": PHASE_LABEL.get(key, ""),
        "topic": game_topic_for(st.get("topic", ""), key, g),
    }


def strategy_evidence(st: dict) -> dict:
    tid = ((st.get("scene") or {}).get("topic_id") or "").strip()
    kb = engine.load_knowledge(tid) if tid else {}
    if not kb and is_ai_topic(st.get("topic", "")):
        kb = engine.load_knowledge("compute-chip")
    blocks = [kb.get("common") or {}]
    player = st.get("you_key") or "cn"
    blocks.append((kb.get("parties") or {}).get(player) or {})
    pool = []
    for block in blocks:
        for slot in ("positions", "actions", "numbers"):
            for card in block.get(slot) or []:
                if isinstance(card, dict) and card.get("text"):
                    pool.append(card)
    if not pool:
        return {}
    card = pool[(st.get("round") or 0) % len(pool)]
    return {k: str(card.get(k) or "") for k in ("id", "date", "text", "source", "link", "conf")}


def all_strategy_cards(st: dict) -> list[dict]:
    topic = st.get("topic") or "本议题"
    ev = strategy_evidence(st)
    fact_text = str(ev.get("text") or "").strip().rstrip("。")
    short_fact = fact_text[:58] + ("…" if len(fact_text) > 58 else "")
    if fact_text:
        fact_desc = "援引知识库记录「" + short_fact + "」，要求对方以可核查事实回应，而不是继续扩大解释。"
        fact_speech = (
            "贵方强调规则与执行，我方原则上并不回避，但任何规则解释都必须经得起事实检验。" +
            (ev.get("source") or "现有资料") + (ev.get("date") and "于" + ev["date"] or "") +
            "的记录明确指出，" + fact_text + "。这一事实与「" + topic + "」直接相关。"
            "因此，我方建议双方先就适用规则、案例边界和可核查标准形成共同清单，再讨论进一步义务。"
            "也请贵方具体说明，目前主张所依据的是哪一项规则或数据，我们不能以模糊判断替代共同确认的事实。")
    else:
        fact_desc = "要求双方提供可核查的规则、案例与数据，先确认事实基础，再讨论具体承诺。"
        fact_speech = ("贵方提出了自己的判断，我方愿继续讨论，但希望先把事实基础确认清楚。"
                       "关于「" + topic + "」，请双方分别列出所依据的规则、案例与数据，并说明适用范围。"
                       "在此基础上，我方愿讨论共同清单和后续安排，避免以未经证实的判断代替正式承诺。")
    return [
        {"id": "fact", "name": "事实援引", "en": "EVIDENCE", "kind": "论据策略",
         "cost": 1, "icon": "book", "color": "#82a9ce", "move": "事实论证",
         "desc": fact_desc, "speech": fact_speech, "deltas": [1, 2, 1],
         "evidence": ev or None},
        {"id": "exchange", "name": "对等交换", "en": "RECIPROCITY", "kind": "合作策略",
         "cost": 2, "icon": "scale", "color": "#c9a061", "move": "条件合作",
         "desc": "接受有限且有边界的安排，同时要求对方给出同步、明确且可验证的对等回报。",
         "speech": "我方理解贵方在「" + topic + "」上的关切，也愿意以实际行动回应，但合作不能变成单方承担义务。"
                   "我方建议采取同步安排：先明确双方各自需要履行的步骤，再确定完成时限、核查方式和未履约时的处理办法。"
                   "只要贵方给出可验证的对等回报，我方就愿讨论相应的具体安排，并将双方承诺写入同一份文本。请贵方说明能够拿出什么对价。",
         "deltas": [4, 1, 4]},
        {"id": "redline", "name": "红线声明", "en": "RED LINE", "kind": "强硬策略",
         "cost": 1, "icon": "shield", "color": "#d98d7c", "move": "坚守底线",
         "desc": "明确拒绝单方扩大义务，要求对方先修正不合理前提。守住边界，但会损害会场氛围。",
         "speech": "对于任何不受约束、单方面扩大我方义务的安排，我方都不能接受。这里需要说清楚，这不是拒绝谈判，而是谈判必须建立在可接受的前提上。"
                   "在「" + topic + "」上，我们仍然可以讨论具体方案，但必须先确认对等、有限和不可滥用的原则，并给出清晰的适用边界。"
                   "请贵方先修正这一前提，否则后续条款无法进入实质磋商。",
         "deltas": [2, -3, -4]},
        {"id": "probe", "name": "试探气球", "en": "PROBE", "kind": "试探策略",
         "cost": 1, "icon": "probe", "color": "#a997c5", "move": "试探回应",
         "desc": "暂不正式承诺，先追问执行主体、标准和披露范围，测试对方真正的可接受边界。",
         "speech": "目前双方对「" + topic + "」的原则表述仍有距离，在作出正式承诺之前，我方希望贵方先回答三个具体问题：由谁执行，依据什么标准，结果向谁披露。"
                   "这些问题决定方案是否真正可执行，也决定双方需要承担多大义务。贵方作出明确说明后，我方愿立即评估下一步安排，而不是让承诺停留在原则口号上。",
         "deltas": [1, 0, 2]},
        {"id": "concede", "name": "有限让步", "en": "CONCESSION", "kind": "合作策略",
         "cost": 1, "icon": "dove", "color": "#86c48f", "move": "有限让步",
         "desc": "仅在低风险、非核心事项上释放善意，以小范围试点换取对方降低整体要求。",
         "speech": "为推动「" + topic + "」取得实质进展，我方愿先迈出一步，在低风险、非核心事项上开展小范围试点，并共同评估试点结果。"
                   "但需要强调，这不等于接受贵方的全部要求，也不能自动扩大适用范围。作为对应行动，贵方应同步降低其他限制。"
                   "如果试点证明安排有效且没有外溢影响，我方愿继续讨论扩大合作。",
         "deltas": [3, 2, 5]},
        {"id": "package", "name": "一揽子方案", "en": "PACKAGE DEAL", "kind": "终局策略",
         "cost": 2, "icon": "package", "color": "#efd7a8", "move": "方案整合",
         "desc": "把核心争议、执行安排和争议复核打包成整体文本，避免逐项僵持，推动可执行协议。",
         "speech": "继续逐项僵持只会消耗双方的谈判空间，因此我方建议把「" + topic + "」作为一个整体处理。"
                   "第一，共同确认核心争议的适用边界；第二，同步写明双方的执行步骤、时限与责任；第三，建立争议复核机制，确保出现问题时能够回到磋商而不是直接对抗。"
                   "这三部分相互依存，我方愿在此基础上讨论完整文本，也请贵方给出整体回应。",
         "deltas": [6, 2, 5]},
    ]


def strategy_cards(st: dict) -> list[dict]:
    cards = all_strategy_cards(st)
    start = 0 if (st.get("round") or 0) % 2 == 0 else 3
    return cards[start:start + 3]


def strategy_card_meta(st: dict, card_id: str) -> dict | None:
    for card in all_strategy_cards(st):
        if card["id"] == card_id:
            return {k: card[k] for k in ("id", "name", "en", "kind", "icon", "move")}
    return None


def public_state(st: dict) -> dict:
    out = {k: v for k, v in st.items() if k not in ("history", "touched", "event_bag")}
    out["offline"] = not current_key()
    out["live"] = bool(FIXED_SID)
    out["game_current"] = current_game_info(st)
    # 前端右上角的「HiAgent / DeepSeek / 离线」标识
    out["brain"] = _brain_status(engine)
    if st.get("mode") == "duel":
        out["strategy_cards"] = strategy_cards(st)
        out["action_points"] = {"max": 2, "remaining": 2}
    if st["finished"]:
        out["elapsed"] = st["elapsed"]
    elif st["started_at"]:
        out["elapsed"] = int(time.time() - st["started_at"])
    else:
        out["elapsed"] = 0
    return out


def now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def limit_speech(text: str, limit: int = SPEECH_LIMIT) -> str:
    """把单条发言压到 limit 字以内：优先在句末断开，实在不行才硬切。"""
    t = (text or "").strip()
    if limit <= 0 or len(t) <= limit:
        return t
    head = t[:limit]
    best = -1
    for ch in ("。", "！", "？", "；", "…"):
        i = head.rfind(ch)
        if i >= int(limit * 0.55) and i > best:
            best = i
    if best > 0:
        return head[:best + 1]
    return head[:max(0, limit - 1)].rstrip("，、,;；:： ") + "…"


TASK_JUDGE_PROMPT = (
    "你是外交谈判教练。判断「玩家本轮发言」是否在实质上推进了下列隐藏任务。\n"
    "判定标准：发言里必须出现明确动作、方案或承诺（例如提出机制、给出数字、作出让步、"
    "争取到某一表述），才算推进；寒暄、复述、泛泛而谈都不算。\n"
    "输出 JSON：{\"done\": [任务序号...], \"reason\": \"一句话说明\"}\n"
    "任务序号从 0 开始；没有推进就输出空数组。只输出 JSON，不要解释。"
)


def judge_tasks_async(st: dict, user_text: str, obj: dict, ctx: dict) -> None:
    """每轮结束后开一个后台线程判定隐藏任务，不拖慢前端拿回复。

    判定结果直接写回 st["tasks"]，前端轮询 /api/state 时会看到任务变绿。
    """
    if not TASK_JUDGE or not current_key():
        return
    if st.get("mode") == "observe":
        return
    tasks = st.get("tasks") or []
    pending = [i for i, t in enumerate(tasks) if not t.get("done")]
    if not pending:
        return

    payload = {
        "议题": st.get("topic") or "",
        "我方": ctx.get("player_label") or "",
        "对手": ctx.get("agent_label") or "",
        "本轮我方发言": user_text,
        "对手回应摘要": (obj.get("response") or "")[:300],
        "隐藏任务": [
            {"序号": i, "任务": tasks[i].get("text", ""), "优先级": tasks[i].get("priority", "")}
            for i in pending
        ],
    }

    def run() -> None:
        try:
            raw, _finish = engine.call_deepseek(
                [{"role": "system", "content": TASK_JUDGE_PROMPT},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                json_mode=True, temperature=0.2)
            out = engine.extract_json(raw)
            ids = out.get("done") or []
            changed = False
            for raw_id in ids:
                try:
                    i = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(tasks) and not tasks[i].get("done"):
                    tasks[i]["done"] = True
                    changed = True
            if changed:
                st["touched"] = time.time()
                print("  [任务] 本轮判定完成：" + "、".join(
                    tasks[i].get("text", "") for i in range(len(tasks)) if tasks[i].get("done")))
        except SystemExit:
            pass
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def topic_meta(st: dict) -> dict:
    tid = ((st.get("scene") or {}).get("topic_id") or "").strip()
    if not tid:
        return {}
    kb = engine.load_knowledge(tid)
    return kb.get("_meta") or {}


_KB_CACHE: dict[str, tuple[float, dict]] = {}


def kb_digest(topic_id: str) -> dict:
    """把议题知识库压成前端够用的摘要：阶段表 + 各方已公开的立场。

    观战页用它做两件事：
      1. 把「外交阶段」画成顶部章节条（替换写死的四幕）
      2. 给每条发言的内心 OS 找一条可引用的知识库事实卡
    只返回 positions，不返回 actions/numbers/quotes/智库，控制体积。
    """
    tid = (topic_id or "").strip()
    if not tid:
        return {"ok": False, "error": "缺少 topicId"}
    path = engine.topic_kb_path(tid)
    if path is None:
        return {"ok": False, "error": "该议题没有知识库文件：" + tid}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    hit = _KB_CACHE.get(tid)
    if hit and hit[0] == mtime:
        return hit[1]

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"知识库读取失败：{e}"}

    def cards(block: dict, limit: int = 12) -> list[dict]:
        out = []
        for c in (block.get("positions") or [])[:limit]:
            if not isinstance(c, dict):
                continue
            out.append({
                "date": str(c.get("date") or ""),
                "text": str(c.get("text") or ""),
                "value": str(c.get("value") or ""),
                "conf": str(c.get("conf") or ""),
            })
        return out

    meta = raw.get("_meta") or {}
    parties = {}
    for k, blk in (raw.get("parties") or {}).items():
        if isinstance(blk, dict):
            parties[str(k)] = {"positions": cards(blk)}
    out = {
        "ok": True,
        "topicId": tid,
        "meta": {
            "id": meta.get("id") or tid,
            "name": meta.get("name") or "",
            "year": meta.get("year") or "",
            "parties": meta.get("parties") or list(parties.keys()),
            "phase_rounds": meta.get("phase_rounds"),
            "phases": meta.get("phases") or [],
        },
        "common": {"positions": cards(raw.get("common") or {}, 8)},
        "parties": parties,
        "counts": {
            "parties": len(parties),
            "cards": sum(len(v["positions"]) for v in parties.values()),
        },
    }
    _KB_CACHE[tid] = (mtime, out)
    return out


def phase_for(st: dict, round_no: int):
    """按历史阶段取发言顺序：阶段内轮值，阶段用完停在最后一个。"""
    meta = topic_meta(st)
    phases = meta.get("phases") or []
    if not phases:
        return None, None
    per = int(meta.get("phase_rounds") or 3)
    idx = min(max(0, (round_no - 1) // per), len(phases) - 1)
    return phases[idx], idx


def speaker_for_round(st: dict, round_no: int) -> str | None:
    """多边 / 观战：对手席按轮次轮值发言；双边固定第一位。"""
    rivals = [r for r in (st.get("rivals") or []) if r]
    if not rivals:
        return None
    if st.get("mode") in ("multi", "observe") and len(rivals) > 1:
        # 有历史阶段（观战史料）就按阶段内的顺序走
        ph, _idx = phase_for(st, round_no)
        if ph:
            order = [c for c in (ph.get("order") or []) if c in rivals]
            if order:
                return order[(round_no - 1) % len(order)]
        # 没有阶段就按原顺序轮值
        candidates = [r for r in rivals if r != st.get("you_key")] or rivals
        return candidates[(round_no - 1) % len(candidates)]
    return rivals[0]


def _do_turn(st: dict, user_text: str, auto: bool = False, card_id: str = "", card_ids=None) -> dict:
    if st["finished"]:
        return {"error": "谈判已结束，请点击「重新开始」再开一局"}

    round_no = st["round"] + 1
    if st["started_at"] is None:
        st["started_at"] = time.time()          # 计时从这里开始
        st["messages"].append({"role": "system", "text": "第 1 轮谈判开始 · 计时已启动"})

    speaker = speaker_for_round(st, round_no)
    ctx = scene_ctx(st.get("scene"), speaker=speaker)
    st["speaker_key"] = ctx["agent_key"]
    _ph, _pi = phase_for(st, round_no)
    if _ph:
        st["phase"] = _ph.get("label") or ""
        st["phase_index"] = _pi
    st["speaker_label"] = ctx["agent_label"]
    st["agent_label"] = ctx["agent_label"]
    st["agent_flag"] = ctx["agent_flag"]

    # 观战九方：每位发言方都从自己的 party_indexes 出发，前面事件扣的分才会进入他的上下文
    observing = st.get("mode") == "observe"
    if observing:
        speaker_state = (st.get("party_indexes") or {}).get(st["speaker_key"])
        if isinstance(speaker_state, dict) and speaker_state:
            st["agent_indexes"] = {k: float(speaker_state.get(k, 50)) for k in engine.INDEX_KEYS}

    ev = maybe_event(st)
    impacts = resolve_event_impacts(st, ev, round_no=round_no) if (ev and observing) else []

    # 本轮事件对发言方的即时影响只进提示词，不进 prev_a；事件结算仍放在本轮发言之后
    prompt_indexes = dict(st["agent_indexes"])
    if ev:
        if observing:
            for imp in impacts:
                if st["speaker_key"] in (imp.get("keys") or []):
                    for field, delta in (imp.get("deltas") or {}).items():
                        prompt_indexes[field] = max(0.0, min(100.0, float(prompt_indexes.get(field, 50)) + delta))
        else:
            eff = (ev.get("effects") or {}).get("agent") or {}
            for k, dk in zip(engine.INDEX_KEYS, engine.DELTA_KEYS):
                prompt_indexes[k] = max(0, min(100, int(prompt_indexes.get(k, 50)) + int(eff.get(dk, 0) or 0)))

    if auto:
        user_text = ""
        ask = ("现在轮到【" + ctx["agent_label"] + "】发言。请代表本方陈述立场、回应前几轮各方的主张，"
               "并推进议程。不要提及这是模拟或自动推演。")
        user_content = engine.build_user_message(round_no, prompt_indexes, ask)
    else:
        user_content = engine.build_user_message(round_no, prompt_indexes, user_text)
    if ev:
        ev_text = str(ev.get("text") or "").strip().rstrip("。；; ")
        if ev.get("no_effect"):
            user_content += ("\n\n【本轮会场花絮】" + ev["title"] + "：" + ev_text
                             + "。\n这是与本议题无直接关联的会场花絮，不改变任何代表团指数；"
                               "可以一句带过，不必强行关联到谈判条款。")
        else:
            effect_text = event_impact_text(impacts) if observing else legacy_event_effect_text(ev, st)
            user_content += "\n\n【本轮突发事件】" + ev["title"] + "：" + ev_text
            if effect_text:
                user_content += "。影响：" + effect_text
            user_content += ("。\n你的发言必须明确回应这一事件：点明它如何改变本方的判断、要价或条件，"
                             "不要只复述事件标题。")
    recent = recent_event_lines(st, 3)
    if recent:
        user_content += ("\n\n【会场近期事件（供引用）】\n"
                         + "\n".join("- " + line for line in recent)
                         + "\n如果本轮与其中某件相关，请点名回应；无关的花絮只需一句带过。")
    prompt = system_prompt_now(st.get("scene"), speaker=speaker)
    msgs = ([{"role": "system", "content": prompt}]
            + st["history"]
            + [{"role": "user", "content": user_content}])

    key = current_key()
    if key:
        os.environ["DEEPSEEK_API_KEY"] = key

    if not key:
        obj = json.loads(json.dumps(
            engine.OFFLINE_RESPONSES[(round_no - 1) % len(engine.OFFLINE_RESPONSES)]))
        raw, finish = "", "offline"
    else:
        try:
            obj, raw, finish = engine.call_with_retry(msgs, prompt, SHIM)
        except SystemExit as e:
            return {"error": f"模型调用失败：{e}"}
        except Exception as e:
            return {"error": f"模型调用异常：{type(e).__name__}: {e}"}

    if obj is None:
        return {"error": f"模型连续 3 次没有返回合法 JSON（{engine.describe_bad(raw, finish)}）"}

    # 模型偶尔会吐一个空 response（尤其被要求「别铺陈」之后）：先重试一次，再兜底
    if not (obj.get("response") or "").strip():
        try:
            retry_msgs = msgs + [{"role": "user",
                           "content": "上一轮输出里的 response 是空的。请按同样的 JSON 格式重写，response 必须有实际内容。"}]
            obj2, _raw2, _fin2 = engine.call_with_retry(retry_msgs, prompt, SHIM)
            if isinstance(obj2, dict) and (obj2.get("response") or "").strip():
                obj = obj2
                print("  [发言] 上一轮 response 为空，已自动重试一次")
        except Exception:
            pass
    obj["_round"] = round_no
    obj["response"] = limit_speech(obj.get("response") or "")
    prev_a = dict(st["agent_indexes"])
    prev_p = dict(st["player_indexes"])
    empty_speech = False
    try:
        obj, warns, new_a = engine.validate_and_normalize(obj, prev_a)
        obj, p_warns, new_p = engine.apply_player_indexes(obj, prev_p)
        warns.extend(p_warns)
        empty_speech = not (obj.get("response") or "").strip()
        if empty_speech:
            obj["response"] = "本轮没有产出有效发言"
            warns.append("模型本轮没有产出有效发言")
    except Exception as e:
        return {"error": f"校验失败：{e}"}

    if auto:
        # 观战没有「我方」，不参与双边博弈结算；只更新发言方自己的指数
        gres = None
        _pi = st.setdefault("party_indexes", {})
        def _clamp(v, lo=10, hi=92):
            try:
                return max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                return 50.0

        _spk = st["speaker_key"]
        _pi = st.setdefault("party_indexes", {})
        _before = {k: dict(v) for k, v in _pi.items()}
        _cur = _pi.get(_spk) or {"interest": 50, "image": 50, "trust": 50}
        # 发言方：只走一部分（模型常给极端值，全盘接受会很快顶满）
        _pi[_spk] = {f: round(_clamp(_cur[f] + 0.72 * (
            float(new_a.get(f, 50) or 50) - _cur[f])), 1)
            for f in ("interest", "image", "trust")}
        # 其他方：缓慢回归 50，避免一直冻结在初始值
        for _k, _v in _pi.items():
            if _k == _spk:
                continue
            for f in ("interest", "image", "trust"):
                _v[f] = round(_clamp(_v[f] + 0.12 * (50 - _v[f])), 1)
        # 记录本轮增减，前端直接展示（轮询重绘不会清零）
        _pd = st.setdefault("party_delta", {})
        for _k, _v in _pi.items():
            _b = _before.get(_k) or _v
            _pd[_k] = {f: round(_v[f] - _b[f], 1) for f in ("interest", "image", "trust")}
    else:
        gres = resolve_game(st, obj, round_no, user_text, prev_a, prev_p, ctx)
        new_a["interest"] = gres["agent_interest"]
        new_p["interest"] = gres["player_interest"]

    if not auto:
        player_message = {"role": "player", "sender": ctx["player_label"],
                          "key": ctx["player_key"],
                          "text": user_text, "chars": len(user_text), "time": now_hm()}
        ids = list(card_ids or ([card_id] if card_id else []))
        cards = [strategy_card_meta(st, cid) for cid in ids]
        cards = [card for card in cards if card]
        if cards:
            player_message["cards"] = cards
            if len(cards) == 1:
                player_message["card"] = cards[0]
        st["messages"].append(player_message)
    _tid = (st.get("scene") or {}).get("topic_id") or ""
    fc = factcheck.check(obj["response"], factcheck.load_kb(_tid or None))
    st["fact_stats"]["checked"] += fc.get("checked", 0)
    st["fact_stats"]["flagged"] += len(fc.get("flagged", []))
    st["messages"].append({"role": "agent", "sender": f"{ctx['agent_label']}代表",
                           "key": ctx["agent_key"],
                           "text": obj["response"], "chars": len(obj["response"]),
                           "no_content": empty_speech,
                           "time": now_hm(), "fact_check": fc})

    hist_obj = {k: v for k, v in obj.items() if not k.startswith("_")}
    st["history"].append({"role": "user", "content": user_content})
    st["history"].append({"role": "assistant",
                          "content": json.dumps(hist_obj, ensure_ascii=False)})

    st["round"] = round_no
    st["agent_indexes"] = new_a
    st["player_indexes"] = new_p

    if gres:
        st["game_log"].append(gres["record"])
        st["last_game"] = gres["record"]
        st["payoff_total"]["player"] += gres["record"]["payoff"]["player"]
        st["payoff_total"]["agent"] += gres["record"]["payoff"]["agent"]

    if ev:
        record = {"round": round_no, "id": ev["id"], "title": ev["title"],
                  "text": ev["text"], "effects": ev.get("effects") or {}}
        # 只有观战九方场才有 impacts；双边 / 多边继续用 effects.player / effects.agent
        if observing:
            record["impacts"] = impacts
        if ev.get("no_effect"):
            record["no_effect"] = True
        st["events"].append(record)
        apply_event(st, ev, impacts)
        # 插在「我方发言」之后、「对手发言」之前
        st["messages"].insert(len(st["messages"]) - 1, {
            "role": "system",
            "text": "⚡ 突发：" + ev["title"] + " · " + ev["text"],
        })

    ss = obj.get("speaker_score")
    st["last"] = {
        "score": ss["score"] if isinstance(ss, dict) else None,
        "dims": ss["dimension"] if isinstance(ss, dict) else {},
        "feedback": ss.get("feedback", "") if isinstance(ss, dict) else "",
        "mood": obj.get("mood", ""),
        "summary": obj.get("round_summary", ""),
    }

    st["assistant"] = call_advisor(st, obj, ev)

    # 隐藏任务判定放到后台，不阻塞本轮返回
    try:
        judge_tasks_async(st, user_text, obj, ctx)
    except Exception:
        pass

    limit = int(st.get("max_rounds") or 0)
    if limit > 0 and round_no >= limit:
        st["finished"] = True
        st["elapsed"] = int(time.time() - st["started_at"])
        st["messages"].append({
            "role": "system",
            "text": f"谈判结束 · 共 {limit} 轮，用时 {st['elapsed'] // 60:02d}:{st['elapsed'] % 60:02d}",
        })

    return {
        "state": public_state(st),
        "deltas": {"agent": obj["indexes_update"], "player": obj["player_indexes_update"]},
        "added": 1 if auto else 2,
        "warnings": warns + ([] if key else ["离线演示模式：对手回复为内置示例"]),
    }


def do_turn(st: dict, user_text: str, auto: bool = False, card_id: str = "", card_ids=None) -> dict:
    """包一层：整轮处理期间给前端一个「对方正在发言」的信号。"""
    st["pending"] = st["round"] + 1
    try:
        return _do_turn(st, user_text, auto=auto, card_id=card_id, card_ids=card_ids)
    finally:
        st["pending"] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "NegotiationDemo/1.8"

    def log_message(self, fmt, *args):
        pass

    def _cookie_sid(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v:
                return v
        return None

    def _with_session(self):
        return get_or_create(self._cookie_sid())

    def _peek_session(self):
        """读状态时不拿 SESS_LOCK，避免整轮等待期间轮询被卡住（直播需要实时性）。"""
        key = FIXED_SID or self._cookie_sid()
        return key, (SESSIONS.get(key) if key else None)

    def _send(self, code, body: bytes, ctype, sid=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if sid:
            self.send_header("Set-Cookie", f"sid={sid}; Path=/; Max-Age=86400; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload, sid=None):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", sid)

    def _static(self, rel: str, sid=None):
        rel = unquote(rel).lstrip("/")
        target = (SITE / rel).resolve()
        try:
            target.relative_to(SITE.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._json(404, {"error": f"not found: {rel}"})
            return
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix.lower(), "application/octet-stream"), sid)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._static(HOME_PAGE, )
            return
        if path == "/api/state":
            sid, st = self._peek_session()
            created = False
            if st is None:
                sid, st, created = self._with_session()
            self._json(200, public_state(st), sid if created else None)
            return
        if path == "/api/kb":
            tid = (parse_qs(urlparse(self.path).query).get("topicId") or [""])[0]
            self._json(200, kb_digest(tid))
            return
        if path == "/api/brain":
            self._json(200, _brain_status(engine))
            return
        if path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if FIXED_SID:
            # 共用一个会话时，静态资源不需要建会话/加锁，避免模型调用期间页面卡住
            self._static(path)
            return
        sid, _, created = self._with_session()
        self._static(path, sid if created else None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._json(400, {"error": "请求体不是合法 JSON"})
            return

        sid, st, created = self._with_session()
        cookie = sid if created else None

        if self.path == "/api/say":
            text = (payload.get("text") or "").strip()
            auto = bool(payload.get("auto")) and (st.get("mode") == "observe")
            if not text and not auto:
                self._json(400, {"error": "发言不能为空"}, cookie)
                return
            card_id = str(payload.get("card_id") or "").strip()
            raw_ids = payload.get("card_ids")
            card_ids = raw_ids if isinstance(raw_ids, list) else ([card_id] if card_id else [])
            card_ids = [str(cid).strip() for cid in card_ids if str(cid).strip()]
            if st.get("mode") == "duel" and card_ids:
                allowed = {card["id"]: int(card["cost"]) for card in strategy_cards(st)}
                if len(card_ids) > 2 or len(set(card_ids)) != len(card_ids):
                    self._json(400, {"error": "每轮最多使用两张不同的策略卡"}, cookie)
                    return
                if any(cid not in allowed for cid in card_ids):
                    self._json(400, {"error": "只能使用当前手牌中的策略卡"}, cookie)
                    return
                if sum(allowed[cid] for cid in card_ids) > 2:
                    self._json(400, {"error": "行动点不足：本轮最多消耗 2 点"}, cookie)
                    return
            with SESS_LOCK:
                result = do_turn(st, text, auto=auto, card_id=card_id, card_ids=card_ids)
            self._json(200, result, cookie)
        elif self.path == "/api/brain/ping":
            brain = getattr(engine, "HIAGENT_BRAIN", None)
            if brain is None:
                self._json(400, {"ok": False, "error": "当前不是 HiAgent 模式"}, cookie)
            else:
                self._json(200, brain.ping(), cookie)
        elif self.path == "/api/session":
            agent = str(payload.get("rival") or "us")
            rivals = payload.get("rivals")
            if not isinstance(rivals, list) or not rivals:
                # 只传 rival 时，对手要真的变成这个人（否则会被 DEFAULT_SCENE 的 ["us"] 覆盖）
                rivals = [agent]
            sc = {
                "agent": agent,
                "player": payload.get("you") or "cn",
                "rivals": rivals,
                "mode": (payload.get("mode") or "").strip(),
                "topic": (payload.get("topic") or "").strip(),
                "topic_id": (payload.get("topicId") or "").strip(),
            }
            rounds = payload.get("rounds")
            with SESS_LOCK:
                SESSIONS[sid] = initial_state(sc)
                if rounds not in (None, ""):
                    try:
                        SESSIONS[sid]["max_rounds"] = max(1, min(30, int(rounds)))
                    except (TypeError, ValueError):
                        pass
            self._json(200, {"ok": True, "state": public_state(SESSIONS[sid])}, cookie)
        elif self.path == "/api/reset":
            with SESS_LOCK:
                keep = SESSIONS.get(sid, {}).get("scene")
                SESSIONS[sid] = initial_state(keep)
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


def main() -> None:
    if "--check-key" in sys.argv:
        print()
        print("=" * 60)
        print("  API Key 检查")
        print("=" * 60)
        env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        print(f"  环境变量 : {'已设置（长度 %d）' % len(env) if env else '未设置'}")
        f = BASE / "key.txt"
        print(f"  key.txt  : {'存在（长度 %d）' % len(f.read_text(encoding='utf-8-sig').strip()) if f.exists() else '不存在'}")
        print(f"             应在 {f}")
        print("=" * 60)
        print("  结果：" + ("在线模式，会调用真实模型" if current_key()
                           else "离线演示模式（对手回复是内置示例）"))
        print()
        return

    print()
    print("=" * 64)
    print("  外交谈判 Demo 1.8 · 本地服务")
    print("=" * 64)
    print(f"  谈判主题    {SCENARIO.get('name', '')}")
    print(f"  当前议题    {TOPIC}")
    print(f"  双方        {PLAYER_LABEL}（你）  vs  {AGENT_LABEL}（对手）")
    print(f"  轮次上限    {MAX_ROUNDS} 轮（到点自动结束并冻结计时）")
    print("  模式        双边 / 多边轮值 / 观战自动推演")
    print("  观战页      章节条 · 解说席 · 内心 OS · 实时思维导图")
    print("  知识库      " + (", ".join(sorted({p.stem for p in (BASE / "knowledge" / "topics").glob("*.json")}))
                              if (BASE / "knowledge" / "topics").is_dir() else "（未找到 topics）"))
    print(f"  隐藏任务    已展示，并由模型每轮判定完成情况")
    if getattr(engine, "HIAGENT_BRAIN", None) is not None:
        _b = engine.HIAGENT_BRAIN.status()
        print(f"  大脑        HiAgent 工作流 · {_b['base_url']}")
        print(f"              Apikey {_b['api_key']} · 超时 {engine.HIAGENT_BRAIN.cfg.get('timeout')}s")
    else:
        print(f"  模型        {engine.MODEL if current_key() else '离线演示（未检测到 API Key）'}")
    print(f"  Agent 引擎  {_ENGINE_DIR}")
    print()
    print(f"  本机访问    http://127.0.0.1:{PORT}")
    print(f"  点击版      http://127.0.0.1:{PORT}/click/")
    ips = lan_ips() if HOST in ("0.0.0.0", "") else [HOST]
    if ips:
        print("  局域网访问  同一 WiFi 的电脑/手机打开：")
        for ip in ips:
            print(f"              http://{ip}:{PORT}")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 64)
    print()

    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"  ⚠ 端口 {PORT} 已被占用，可能已经有一个 Demo 在运行（请确认它是 1.6）。")
        print(f"     直接打开  http://127.0.0.1:{PORT}")
        print(f"     或关闭占用端口的旧进程（可能是 webdemo 1.4 / 1.3）后重新启动。")
        if "--no-browser" not in sys.argv:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        return

    httpd.serve_forever()


if __name__ == "__main__":
    main()
