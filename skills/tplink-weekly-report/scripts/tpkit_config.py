# -*- coding: utf-8 -*-
"""TP-LINK 清单工具集 · 统一路径 / 凭据配置。

为什么有这个文件
----------------
原先各脚本把作者的本地绝对路径（形如 D:\\lzy\\...）写死在代码里，
换一台电脑就找不到文件。本模块把这类路径和账号统一收进一份 JSON：

    代码只管"读配置"，配置由使用者按自己的机器填写。
    本机未做任何配置时，可写类数据（价格备份 / 选型备份 / License 映射）
    会自动落到 ~/.workbuddy/tplink-toolkit/data/ 下，仍可正常使用。

配置读取顺序（先命中先用，只取第一个存在的文件）
---------------------------------------------
1) 环境变量 TPKIT_CONFIG 指向的 JSON 文件
2) 技能根目录/config.local.json      （scripts/ 的上一级）
3) 技能 scripts/config.local.json
4) 用户级 ~/.workbuddy/tplink-toolkit.config.json   ← 推荐用这个

配置字段（全部可选，留空的项按「留空行为」处理）
------------------------------------------------
  kongbiao_json       控标参数库 JSON              留空→提示未配置，参数改走官网
  fw_spec_json        防火墙/行为管理官方规格 JSON  留空→核算退回控标库
  dealer_backup_json  选型（合作伙伴平台）备份 JSON 留空→选型没有候选可挑（最关键）
  price_backup_json   CRM 价格本地备份 JSON        留空→落到默认 data 目录
  license_map_json    多合一 License 映射 JSON     留空→落到默认 data 目录
  weekly_report_root  周报根目录                   留空→周报技能提示未配置
  crm_base            内部 CRM 站点根地址          留空→取价跳过，参数不受影响
  dealer_base         合作伙伴平台根地址           留空→仅平台类操作受影响
  pricing_default     未说明定价策略时的默认策略    留空→每次报价仍会问
  crm:    {email, password}   也可用环境变量 TP_CRM_EMAIL / TP_CRM_PASSWORD
  dealer: {phone, password}   也可用环境变量 TP_DEALER_PHONE / TP_DEALER_PASSWORD

每个字段的详细含义、数据文件从哪来、留空的具体表现，
见仓库根 config.example.json（每字段上方有一条 `_字段名` 注释，JSON 不支持注释故用此约定）。

给 AI 的配置指引
----------------
要让 AI 帮忙配置，把上面的「配置字段」和 config.example.json 交给它即可；
更完整的五步流程（看现状→分支→写文件→验证→讲边界）写在
skills/tplink-quote-autofill/SKILL.md 的「🤖 帮用户配好配置」一节。
三条铁律：路径用正斜杠；已存在的配置先读再改（别整份覆盖，会冲掉账号）；
拿不到的值留空，绝不填假路径（留空报"未配置"，假路径报"文件不存在"，后者更难查）。

内部系统地址为什么不写死在代码里
--------------------------------
CRM 与合作伙伴平台都是组织内部系统。为了让本仓库可以公开分发，
代码里**不保留任何真实域名**，一律从 crm_base / dealer_base 读取。
未配置时相关功能会明确提示"未配置"，而不是静默失败。

自查当前生效配置：
    python tpkit_config.py
"""
import json
import os
import sys

_ENV = "TPKIT_CONFIG"
_USER_CFG = os.path.join(os.path.expanduser("~"), ".workbuddy",
                         "tplink-toolkit.config.json")
_DATA_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy",
                         "tplink-toolkit", "data")

# 路径型配置项 → 未配置时自动落盘用的文件名（None = 无兜底，必须配置）
_PATH_KEYS = {
    "kongbiao_json":     None,
    "fw_spec_json":      None,
    "dealer_backup_json": "tplink_dealer_backup.json",
    "price_backup_json":  "tplink_crm_price_backup.json",
    "license_map_json":   "tplink_license_map.json",
}


def _candidate_files():
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    if os.environ.get(_ENV):
        out.append(os.environ[_ENV])
    out.append(os.path.join(os.path.dirname(here), "config.local.json"))
    out.append(os.path.join(here, "config.local.json"))
    out.append(_USER_CFG)
    return out


def _load():
    cfg = {"crm": {}, "dealer": {}}
    used = None
    for p in _candidate_files():
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:                       # 配置坏了也要能跑
            print("[配置] 读取失败 %s: %s" % (p, exc), file=sys.stderr)
            continue
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        used = p
        break
    return cfg, used


_CFG, _USED = _load()


def raw():
    """完整配置 dict（含 crm / dealer 子项）。"""
    return _CFG


def config_file():
    """实际生效的配置文件路径；None 表示未找到任何配置文件。"""
    return _USED


def get(key, default=None):
    return _CFG.get(key, default)


def _missing_msg(key):
    return (
        "[%s] 未配置，无法定位文件。\n"
        "请任选一种方式指定：\n"
        "  1) 在 %s 里写：{\"%s\": \"D:/你的路径/xxx.json\"}\n"
        "  2) 设环境变量 TPKIT_CONFIG 指向你的配置文件\n"
        "  3) 命令行参数直接指定（如 --kb）\n"
        "字段说明见仓库里的 config.example.json。"
        % (key, _USER_CFG, key)
    )


def path(key, must=True):
    """取路径型配置项（展开 ~ 和 %VAR%）。must=True 且缺失时直接报错退出。"""
    val = str(_CFG.get(key) or "").strip()
    if val:
        return os.path.expandvars(os.path.expanduser(val))
    if must:
        raise SystemExit(_missing_msg(key))
    return None


def data_path(key):
    """可写备份类文件：优先用配置；未配置则落到默认 data 目录（并建好该目录）。"""
    p = path(key, must=False)
    if p:
        return p
    os.makedirs(_DATA_DIR, exist_ok=True)
    return os.path.join(_DATA_DIR, _PATH_KEYS[key])


def crm_creds():
    """(email, password) —— 取自配置文件的 crm 段，可能为空。"""
    c = _CFG.get("crm") or {}
    return (c.get("email") or "").strip(), (c.get("password") or "").strip()


def dealer_creds():
    """(phone, password) —— 取自配置文件的 dealer 段，可能为空。"""
    d = _CFG.get("dealer") or {}
    return (d.get("phone") or "").strip(), (d.get("password") or "").strip()


def base(key):
    """内部系统根地址（crm_base / dealer_base）。未配置返回空串，末尾不带斜杠。"""
    return str(_CFG.get(key) or "").strip().rstrip("/")


def pricing_default():
    """未说明定价策略时的默认策略（自然语言）。未配置返回空串（→ 每次仍必问）。"""
    return str(_CFG.get("pricing_default") or "").strip()


class NotConfigured(RuntimeError):
    """必需的配置项缺失。上层可捕获后降级（如改用本地备份）。"""


def require_base(key, what):
    """取内部系统根地址；未配置时抛出 NotConfigured（带填写指引）。"""
    b = base(key)
    if not b:
        raise NotConfigured(
            "未配置%s地址（配置项 %s）。请在你自己的配置文件里填写：\n"
            '    {"%s": "https://<你们组织的域名>"}\n'
            "当前配置文件：%s\n"
            "说明：本仓库不内置任何内部系统域名，需使用者按所在组织填写。"
            % (what, key, key, _USED or "(未找到，可放到 ~/.workbuddy/tplink-toolkit.config.json)"))
    return b


def describe():
    lines = ["生效配置文件: %s" % (_USED or "(未找到，使用默认/兜底路径)")]
    for k in _PATH_KEYS:
        v = path(k, must=False)
        lines.append("  %-20s %s" % (k, v or ("→ %s（默认 data 目录）"
                                              % os.path.join(_DATA_DIR, _PATH_KEYS[k])
                                              if _PATH_KEYS[k] else "未配置")))
    lines.append("  %-20s %s" % ("weekly_report_root", path("weekly_report_root", must=False) or "未配置"))
    lines.append("  %-20s %s" % ("crm_base", base("crm_base") or "未配置"))
    lines.append("  %-20s %s" % ("dealer_base", base("dealer_base") or "未配置"))
    lines.append("  %-20s %s" % ("pricing_default", pricing_default() or "未配置（每次必问）"))
    e, _ = crm_creds()
    lines.append("  %-20s %s" % ("crm.email", e or "未配置"))
    p, _ = dealer_creds()
    lines.append("  %-20s %s" % ("dealer.phone", p or "未配置"))
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(describe())
