# -*- coding: utf-8 -*-
"""自检：确认这套工具在「本机」真的能跑，以及「仓库」里没有不该有的东西。

用法：
    python 自检.py              # 全部检查
    python 自检.py --repo-only  # 只查仓库卫生（push 前用，不需要数据）

设计意图（2026-10-19）：
    本地跑得通 ≠ 同事拉下来跑得通。本机有配置文件、有数据、有 .pyc 缓存，
    这些都掩盖了「缺文件」「路径写死」的问题。所以这里分两组：
      A 组 · 代码卫生   —— 与数据无关，任何 clone 都该过
      B 组 · 运行能力   —— 依赖配置与数据，过不了说明数据没装对

只读检查，不修改任何文件。
退出码：0 全过 / 1 有 FATAL / 2 只有 WARN
"""
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(ROOT, "skills", "tplink-quote-autofill", "scripts")
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(ROOT, "dealer-tool"))

FATAL, WARN, OK = [], [], []


def fatal(msg, hint=""):
    FATAL.append((msg, hint))


def warn(msg, hint=""):
    WARN.append((msg, hint))


def ok(msg):
    OK.append(msg)


def _run_rel(p):
    return os.path.relpath(p, ROOT)


# ---------------------------------------------------------------- A 组
# 这些串一旦出现在仓库里，说明脱敏没做干净
FORBIDDEN = [
    (r"sales\.tp-link\.(net|com)", "内部 CRM 域名"),
    (r"dealer\.tp-link\.com\.cn", "合作伙伴平台域名"),
    (r"\b1[3-9]\d{9}\b", "手机号"),
    (r"[A-Za-z]:[\\/](?:lzy|Users[\\/]admin)", "本机绝对路径"),
    (r"工作在南昌办", "内部共享目录名"),
]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}
SKIP_EXT = {".pyc", ".pyo", ".zip", ".xlsx", ".xls", ".png", ".jpg"}
# 本文件自己就写着这些模式串，跳过，否则每次都自报 1 处
SKIP_FILES = {"自检.py"}
# 已知可公开的：示例配置里的占位符说明、文档里讲"不要写域名"的段落
ALLOW_LINE = re.compile(r"内网域名|你的组织|本仓库不内置|<.*>|\$CRM_BASE|\$DEALER_BASE")


def check_repo():
    print("\n[A] 代码卫生（push 到 GitHub 前必须全过）")
    print("-" * 62)
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT or fn in SKIP_FILES:
                continue
            full = os.path.join(dirpath, fn)
            try:
                with io.open(full, encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if ALLOW_LINE.search(line):
                            continue
                        for pat, why in FORBIDDEN:
                            if re.search(pat, line):
                                hits.append((_run_rel(full), i, why, line.strip()[:70]))
                                break
            except (OSError, UnicodeDecodeError):
                continue

    if hits:
        fatal("仓库里有 %d 处敏感串" % len(hits))
        for f, i, why, txt in hits[:12]:
            print("    %s:%d  [%s]  %s" % (f, i, why, txt))
        if len(hits) > 12:
            print("    …… 另有 %d 处" % (len(hits) - 12))
    else:
        ok("无内部域名 / 手机号 / 本机路径 / 内部目录名")
        print("  OK   无内部域名 / 手机号 / 本机路径 / 内部目录名")

    # 数据文件不能进版本库
    tracked = set()
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8").stdout
        tracked = set(out.splitlines())
    except Exception:
        warn("读不到 git 索引（不是 git 仓库？），跳过「数据文件是否入库」检查")
        print("  WARN 读不到 git 索引，跳过数据文件检查")

    leaked = [f for f in tracked
              if re.match(r"tplink_(?!.*items_nat).*\.json$", os.path.basename(f))
              or os.path.basename(f).endswith((".xlsx", ".xls"))]
    if leaked:
        fatal("数据文件被提交进仓库了：%s" % ", ".join(leaked[:5]))
        print("  FATAL 数据文件入库：%s" % ", ".join(leaked[:5]))
    elif tracked:
        ok("数据文件未入库（%d 个受版本控制文件）" % len(tracked))
        print("  OK   数据文件未入库（%d 个受版本控制文件）" % len(tracked))

    # tpkit_config 的 10 份副本必须一致
    import hashlib
    src = os.path.join(ROOT, "_shared", "tpkit_config.py")
    copies = [
        os.path.join(SCRIPT_DIR, "tpkit_config.py"),
        os.path.join(ROOT, "skills", "tplink-crm-readonly", "scripts", "tpkit_config.py"),
        os.path.join(ROOT, "skills", "tplink-weekly-report", "scripts", "tpkit_config.py"),
        os.path.join(ROOT, "skills", "tplink-crm-eol-check", "tpkit_config.py"),
        os.path.join(ROOT, "dealer-tool", "tpkit_config.py"),
    ]
    if os.path.exists(src):
        h0 = hashlib.md5(open(src, "rb").read()).hexdigest()
        bad = []
        for c in copies:
            if not os.path.exists(c):
                bad.append((_run_rel(c), "缺失"))
            elif hashlib.md5(open(c, "rb").read()).hexdigest() != h0:
                bad.append((_run_rel(c), "内容不一致"))
        if bad:
            fatal("tpkit_config.py 副本不同步：%s" % bad)
            print("  FATAL tpkit_config.py 副本不同步：")
            for f, why in bad:
                print("      %s  %s" % (f, why))
        else:
            ok("tpkit_config.py 5 份副本 md5 一致")
            print("  OK   tpkit_config.py 5 份副本 md5 一致")


# ---------------------------------------------------------------- B 组
def check_runtime():
    print("\n[B] 运行能力（本机装好没、数据对不对）")
    print("-" * 62)

    # Python 版本与依赖
    if sys.version_info < (3, 8):
        fatal("Python 版本过低：%s（需 3.8+）" % sys.version.split()[0])
        print("  FATAL Python 版本过低：%s" % sys.version.split()[0])
    else:
        ok("Python %s" % sys.version.split()[0])
        print("  OK   Python %s" % sys.version.split()[0])

    try:
        import openpyxl
        ok("openpyxl %s" % getattr(openpyxl, "__version__", "?"))
        print("  OK   openpyxl %s" % getattr(openpyxl, "__version__", "?"))
    except ImportError:
        fatal("没装 openpyxl，读写 xlsx 会失败", "pip install openpyxl")
        print("  FATAL 没装 openpyxl → pip install openpyxl")

    # 配置
    try:
        import tpkit_config as cfg
    except Exception as exc:
        fatal("tpkit_config 导入失败：%s" % exc)
        print("  FATAL tpkit_config 导入失败：%s" % exc)
        return

    used = cfg.config_file()
    print("  配置：%s" % (used or "(未找到配置文件，走默认路径)"))
    if not used:
        warn("没找到配置文件", "跑数据包里的 生成配置.py，或参考 config.example.json 手写")
        print("  WARN 没找到配置文件 → 跑数据包里的 生成配置.py")

    # 只读数据源：配了就必须在
    for key, label in (("kongbiao_json", "控标参数库"),
                       ("fw_spec_json", "防火墙规格")):
        v = cfg.path(key, must=False)
        if not v:
            warn("%s 未配置" % label, "选型/参数会退回官网抓取，覆盖少、慢")
            print("  WARN %s 未配置" % label)
        elif not os.path.exists(v):
            fatal("%s 路径已配置但文件不存在：%s" % (label, v),
                  "路径写错了，或数据包没拷到这个位置")
            print("  FATAL %s 文件不存在：%s" % (label, v))
        else:
            try:
                data = json.load(io.open(v, encoding="utf-8"))
                # 控标库是 list；防火墙规格是 dict，型号在 models 里
                if isinstance(data, dict) and isinstance(data.get("models"), (list, dict)):
                    n = len(data["models"])
                elif hasattr(data, "__len__"):
                    n = len(data)
                else:
                    n = 0
                ok("%s %d 条" % (label, n))
                print("  OK   %s %d 条" % (label, n))
            except Exception as exc:
                fatal("%s 读不了（JSON 坏了？）：%s" % (label, exc))
                print("  FATAL %s 读不了：%s" % (label, exc))

    # 选型库
    p = cfg.data_path("dealer_backup_json")
    configured = bool(cfg.path("dealer_backup_json", must=False))
    if not os.path.exists(p):
        if configured:
            fatal("选型库不存在：%s" % p,
                  "选型阶段做不了；从数据包拷 数据/2-选型库.json 到该位置")
            print("  FATAL 选型库不存在：%s" % p)
        else:
            fatal("选型库未配置，也就没有库：%s" % p,
                  "先跑数据包里的 生成配置.py 把路径指好，再把 数据/2-选型库.json 放进去")
            print("  FATAL 选型库未配置（当前是默认空目录）：%s" % p)
    else:
        try:
            d = json.load(io.open(p, encoding="utf-8"))
            ps = d.get("products") or {}
            meta = d.get("_meta") or {}
            ok("选型库 %d 条（更新于 %s）" % (len(ps), meta.get("updated") or meta.get("last_refresh") or "?"))
            print("  OK   选型库 %d 条（更新于 %s）"
                  % (len(ps), meta.get("updated") or meta.get("last_refresh") or "?"))
            if not any(r.get("siblings") for r in ps.values()):
                warn("选型库没有 siblings 字段（旧版数据）",
                     "跑 python dealer-tool/backup.py refresh --deep 补齐同系列信息")
                print("  WARN 选型库是旧版（无同系列信息）")
        except Exception as exc:
            fatal("选型库读不了（JSON 坏了）：%s" % exc)
            print("  FATAL 选型库读不了：%s" % exc)

    # 三项自测
    if not os.path.isdir(SCRIPT_DIR):
        fatal("技能脚本目录不存在：%s" % SCRIPT_DIR)
        print("  FATAL 技能脚本目录不存在")
        return
    for t in ("autofill_quote", "nat_classify", "capacity_check"):
        f = os.path.join(SCRIPT_DIR, t + ".py")
        if not os.path.exists(f):
            fatal("缺脚本 %s.py" % t)
            print("  FATAL 缺脚本 %s.py" % t)
            continue
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", f, "--selftest"],
                               cwd=SCRIPT_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            tail = (r.stdout or "").strip().splitlines()
            line = tail[-1] if tail else "(无输出)"
            if r.returncode == 0 and "通过" in line:
                ok("%s --selftest" % t)
                print("  OK   %-18s %s" % (t, line))
            else:
                fatal("%s --selftest 未通过：%s" % (t, line or (r.stderr or "")[:120]))
                print("  FATAL %-17s %s" % (t, line))
        except subprocess.TimeoutExpired:
            fatal("%s --selftest 超时" % t)
            print("  FATAL %s --selftest 超时" % t)


def check_pricing_default():
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import tpkit_config as cfg
        v = cfg.pricing_default()
    except Exception:
        return
    if v:
        print("\n  定价默认策略：%s" % v)
    else:
        print("\n  定价默认策略：未配置 → 每次报价都会问你")


def main():
    repo_only = "--repo-only" in sys.argv
    print("=" * 62)
    print("TP-LINK 清单工作流 · 自检")
    print("仓库：%s" % ROOT)
    print("=" * 62)

    check_repo()
    if not repo_only:
        check_runtime()
        check_pricing_default()

    print("\n" + "=" * 62)
    print("结论：OK %d 项 / WARN %d 项 / FATAL %d 项"
          % (len(OK), len(WARN), len(FATAL)))
    if FATAL:
        print("\n必须先解决（FATAL）：")
        for m, h in FATAL:
            print("  • %s" % m)
            if h:
                print("    → %s" % h)
    if WARN:
        print("\n建议处理（WARN）：")
        for m, h in WARN:
            print("  • %s" % m)
            if h:
                print("    → %s" % h)
    if not FATAL and not WARN:
        print("\n全部通过，可以交付。")
    elif not FATAL:
        print("\n没有阻断问题，可以跑；WARN 项按需处理。")
    print("=" * 62)
    return 1 if FATAL else (2 if WARN else 0)


if __name__ == "__main__":
    sys.exit(main())
