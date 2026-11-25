#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键安装：把本仓库的技能装进 WorkBuddy，并生成一份初始配置。

用法：
    python install.py            # 安装技能 + 生成/补配配置（终端里会问 KBS 账号）
    python install.py --no-input # 全程不提问（脚本/服务器用）
    python install.py --dry-run  # 只看会做什么，不落盘

装完需要手动做的一件事：编辑生成的配置文件，填上你自己的数据路径。
"""
import argparse
import getpass
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.join(os.path.expanduser("~"), ".workbuddy")
SKILLS_DST = os.path.join(WB, "skills")
TOOLKIT_DIR = os.path.join(WB, "tplink-toolkit")
CFG_DST = os.path.join(WB, "tplink-toolkit.config.json")
CFG_SRC = os.path.join(HERE, "config.example.json")

# 知识库站点地址：已内置。站点只有内网能访问，地址公开无风险；
# 账号密码绝不内置，必须由使用者填自己的。
KBS_BASE_DEFAULT = "https://kbs.tp-link.com.cn"


def copy_skills(dry):
    src_root = os.path.join(HERE, "skills")
    names = sorted(d for d in os.listdir(src_root)
                   if os.path.isdir(os.path.join(src_root, d)))
    for name in names:
        src = os.path.join(src_root, name)
        dst = os.path.join(SKILLS_DST, name)
        print("  %-28s -> %s" % (name, dst))
        if not dry:
            os.makedirs(SKILLS_DST, exist_ok=True)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return names


def copy_dealer(dry):
    src = os.path.join(HERE, "dealer-tool")
    dst = os.path.join(TOOLKIT_DIR, "dealer-tool")
    print("  %-28s -> %s" % ("dealer-tool（选型）", dst))
    if not dry:
        os.makedirs(TOOLKIT_DIR, exist_ok=True)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _ask(label, default=""):
    """读一行输入；回车取默认值。EOF/非交互时返回默认值。"""
    tip = " [%s]" % default if default else ""
    try:
        v = input("%s%s: " % (label, tip)).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return v or default


def _ask_pwd(label, default=""):
    """密码不回显；已有值时空回车表示保留。
    非终端（管道 / 脚本）时改用普通 input —— Windows 的 getpass 会直接读键盘，
    在这种环境下会卡死。"""
    tip = "（回车保留原值）" if default else "（不回显，可留空）"
    try:
        if sys.stdin and sys.stdin.isatty():
            v = getpass.getpass("%s%s: " % (label, tip))
        else:
            v = input("%s%s: " % (label, tip))
    except (EOFError, KeyboardInterrupt):
        return default
    except Exception:
        return default
    return (v or "").strip() or default


def _fill_defaults(cfg):
    """补齐老配置里没有的键，避免升级后 kbs 段根本不存在。"""
    cfg.setdefault("crm", {"email": "", "password": ""})
    cfg.setdefault("dealer", {"phone": "", "password": ""})
    kbs = cfg.get("kbs")
    if not isinstance(kbs, dict):
        kbs = {}
    if not (kbs.get("base") or "").strip():
        kbs["base"] = KBS_BASE_DEFAULT      # 地址补齐；账号密码一律不预填
    kbs.setdefault("user", "")
    kbs.setdefault("password", "")
    cfg["kbs"] = kbs
    for k in ("crm_base", "dealer_base", "pricing_default", "proxy"):
        cfg.setdefault(k, "")


def ask_kbs(cfg):
    """只问 KBS 的账号密码——地址已内置，不用用户填。"""
    kbs = cfg.get("kbs") or {}
    base = (kbs.get("base") or "").strip() or KBS_BASE_DEFAULT
    cur_user = (kbs.get("user") or "").strip()
    print("\n  ── KBS 知识库检索（可选，不配不影响参数/价格/选型）──")
    print("     地址已内置：%s" % base)
    print("     下面填**你自己的**知识库账号密码（登录知识库网页那一套），")
    print("     不要借别人的——检索/下载记录都挂在账号上，借号会混在一起。")
    new_base = _ask("     KBS 地址", base)
    user = _ask("     KBS 账号", cur_user)
    pwd = _ask_pwd("     KBS 密码", kbs.get("password") or "")
    cfg["kbs"] = {"base": (new_base or base).rstrip("/"),
                  "user": user.strip(), "password": pwd}
    if not user.strip():
        print("     账号为空 → KBS 技能暂时用不了，以后重跑 install.py --ask 补上")
    return True


def make_config(dry, ask):
    """生成配置；已存在则不覆盖路径，但允许补配 kbs（老用户升级用）。"""
    if not os.path.exists(CFG_DST):
        print("  %-28s -> %s" % ("初始配置（模板）", CFG_DST))
        if not dry:
            os.makedirs(WB, exist_ok=True)
            shutil.copy2(CFG_SRC, CFG_DST)
    else:
        print("  配置已存在，路径不覆盖：%s" % CFG_DST)
        print("    （只补配缺的项，你填过的值一律不动）")

    if not ask:
        return
    if dry:
        print("  [--dry-run] 跳过交互提问")
        return
    if not os.path.exists(CFG_DST):
        return

    try:
        with open(CFG_DST, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception as exc:
        print("  ⚠ 现有配置读不出来（%s），跳过补配，请手动编辑。" % exc)
        return

    _fill_defaults(cfg)
    if not ask_kbs(cfg):
        return

    bak = CFG_DST + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CFG_DST, bak)
    with open(CFG_DST, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("  ✓ KBS 配置已写入（原文件备份：%s）" % os.path.basename(bak))
    print("    验证：python ~/.workbuddy/skills/tplink-kbs-search/scripts/kbs.py login")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="安装 tplink-quote-toolkit")
    ap.add_argument("--dry-run", action="store_true", help="只演示，不落盘")
    ap.add_argument("--ask", action="store_true",
                    help="装完交互式填写 KBS 账号（终端里默认就会问）")
    ap.add_argument("--no-input", action="store_true",
                    help="全程不提问（脚本 / 服务器 / 管道调用）")
    a = ap.parse_args()

    # 默认：在终端里就问，非交互环境（管道、计划任务）不问，免得卡住
    try:
        tty = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        tty = False
    ask = (a.ask or tty) and not a.no_input

    print("目标目录：%s\n" % WB)
    print("[1/3] 安装技能")
    names = copy_skills(a.dry_run)
    print("\n[2/3] 安装选型工具")
    copy_dealer(a.dry_run)
    print("\n[3/3] 生成配置")
    make_config(a.dry_run, ask)

    print("\n完成，共 %d 个技能。" % len(names))
    print("=" * 62)
    if ask:
        print("剩下一步：把 %s 里的数据路径改成你自己的真实路径。" % CFG_DST)
    else:
        print("接下来：编辑 %s" % CFG_DST)
        print("把里面的路径改成你自己电脑上的真实路径（照 config.example.json 的注释填）。")
        print("要用 KBS 检索，只需填 kbs.user / kbs.password（地址已内置），")
        print("或重跑 python install.py --ask 让它问你。")
    print("然后重启 WorkBuddy，在对话里说「把这个清单的参数和价格补一下」即可。")
    print("=" * 62)


if __name__ == "__main__":
    main()
