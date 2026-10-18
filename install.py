#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键安装：把本仓库的技能装进 WorkBuddy，并生成一份初始配置。

用法：
    python install.py            # 安装技能 + 生成配置（不覆盖已有配置）
    python install.py --dry-run  # 只看会做什么，不落盘

装完需要手动做的一件事：编辑生成的配置文件，填上你自己的数据路径。
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WB = os.path.join(os.path.expanduser("~"), ".workbuddy")
SKILLS_DST = os.path.join(WB, "skills")
TOOLKIT_DIR = os.path.join(WB, "tplink-toolkit")
CFG_DST = os.path.join(WB, "tplink-toolkit.config.json")
CFG_SRC = os.path.join(HERE, "config.example.json")


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


def make_config(dry):
    if os.path.exists(CFG_DST):
        print("  配置已存在，跳过：%s" % CFG_DST)
        return
    print("  %-28s -> %s" % ("初始配置", CFG_DST))
    if not dry:
        os.makedirs(WB, exist_ok=True)
        shutil.copy2(CFG_SRC, CFG_DST)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="安装 tplink-quote-toolkit")
    ap.add_argument("--dry-run", action="store_true", help="只演示，不落盘")
    a = ap.parse_args()

    print("目标目录：%s\n" % WB)
    print("[1/3] 安装技能")
    names = copy_skills(a.dry_run)
    print("\n[2/3] 安装选型工具")
    copy_dealer(a.dry_run)
    print("\n[3/3] 生成配置")
    make_config(a.dry_run)

    print("\n完成，共 %d 个技能。" % len(names))
    print("=" * 62)
    print("接下来只剩一步：编辑 %s" % CFG_DST)
    print("把里面的路径改成你自己电脑上的真实路径（照 config.example.json 的注释填）。")
    print("然后重启 WorkBuddy，在对话里说「把这个清单的参数和价格补一下」即可。")
    print("=" * 62)


if __name__ == "__main__":
    main()
