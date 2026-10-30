# -*- coding: utf-8 -*-
"""
xlsx_lint.py —— 生成/改写 xlsx 后的结构体检（XML 直改必跑）

为什么需要它（2026-10-30 血案）
    清单插列 + 批量写单元格时，用的是「删掉几个格 → 在行尾追加新格」的老办法。
    列位移后**没删干净**，结果同一行出现两个 `I` 单元格（一个空、一个带价），
    列顺序也变成 A,B,C,D,E,F,I,J,K,L,G,H,I,M（乱序）。

    - **openpyxl 读最后一个** → 自查全过，报「已填 384 行」；
    - **WPS / Excel 读第一个**（那个空的）→ 用户看到单价整列空白，以为根本没配。
    用户第 7 版才把我按住：「我看了 46、47 行你都没配」。

    结论：**只信 openpyxl 回读是不够的**。openpyxl 的宽容会掩盖 WPS 的严格。
    交付前必须做「物理层」体检：有没有重复格、列序对不对、样式索引越界没有。

用法
    python xlsx_lint.py 文件.xlsx
    python xlsx_lint.py 文件.xlsx --sheet 清单
    python xlsx_lint.py 文件.xlsx --model-col G --param-col H --price-col I \
                                 --qty-col F --note-col M
    python xlsx_lint.py 文件.xlsx --json          # 机器可读

退出码
    0 = 通过（可能有 WARN）
    2 = 有 FATAL，**不许交付**

设计取舍
    - 不依赖 openpyxl（它太宽容，正是问题来源），直接读 zip 里的 XML。
    - 只做「结构」判断，不做业务判断；业务口径（价格对不对）另有人管。
    - 列参数可选：不传就只做结构检查，传了才查「型号/单价/说明」配对。
"""

import argparse
import json
import re
import sys
import zipfile

CELL_RE = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(/>|>(.*?)</c>)', re.S)
ROW_RE = re.compile(r'<row r="(\d+)"([^>]*)>(.*?)</row>', re.S)


def col_ord(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def read_sheet(path, sheet_name=None):
    """返回 (sheet_xml, styles_xml, workbook_xml, sheet_file, sheet_names)"""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    wb = z.read('xl/workbook.xml').decode('utf-8')
    rels = z.read('xl/_rels/workbook.xml.rels').decode('utf-8') if 'xl/_rels/workbook.xml.rels' in names else ''
    sheets = re.findall(r'<sheet[^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb)
    if not sheets:
        sheets = [(m, '') for m in re.findall(r'<sheet[^>]*name="([^"]+)"', wb)]
    rid2target = dict(re.findall(r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    rid2target.update({k: v for v, k in re.findall(r'<Relationship[^>]*Target="([^"]+)"[^>]*Id="([^"]+)"', rels)})

    target = None
    if sheet_name:
        for nm, rid in sheets:
            if nm == sheet_name:
                target = rid2target.get(rid)
                break
        if target is None:
            raise SystemExit('找不到工作表：%s（现有：%s）' % (sheet_name, '、'.join(n for n, _ in sheets)))
    else:
        target = rid2target.get(sheets[0][1]) if sheets and sheets[0][1] else None
    if target is None:
        target = 'worksheets/sheet1.xml'
    target = target.lstrip('/')
    if not target.startswith('xl/'):
        target = 'xl/' + target
    if target not in names:
        cands = [n for n in names if re.match(r'xl/worksheets/sheet\d+\.xml$', n)]
        if not cands:
            raise SystemExit('压缩包里找不到工作表 XML')
        target = cands[0]
    return z.read(target).decode('utf-8'), z.read('xl/styles.xml').decode('utf-8'), wb, target, [n for n, _ in sheets]


def eval_simple(expr):
    """把 '=4+2' / '12.5' 这类算成数字；算不了返回 None"""
    if expr is None:
        return None
    s = str(expr).strip()
    if s.startswith('='):
        s = s[1:].strip()
        if not re.fullmatch(r'[0-9+\-*/(). ]+', s):
            return None
        try:
            return float(eval(s, {'__builtins__': {}}, {}))
        except Exception:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def cell_text(inner):
    if not inner:
        return ''
    parts = re.findall(r'<t[^>]*>(.*?)</t>', inner, re.S)
    if parts:
        return ''.join(parts)
    m = re.search(r'<v>(.*?)</v>', inner, re.S)
    return m.group(1) if m else ''


def cell_value(inner):
    """数值取 <v>，文本取 <is> 里的 <t>"""
    if not inner:
        return ''
    if '<is>' in inner:
        return cell_text(inner)
    m = re.search(r'<v>(.*?)</v>', inner, re.S)
    return m.group(1) if m else ''


def main():
    ap = argparse.ArgumentParser(description='生成 xlsx 后的结构体检（重复格/列序/样式索引/配对）')
    ap.add_argument('xlsx')
    ap.add_argument('--sheet', default=None, help='工作表名，默认第一张')
    ap.add_argument('--model-col', default=None, help='型号列，如 G')
    ap.add_argument('--param-col', default=None, help='参数列，如 H')
    ap.add_argument('--price-col', default=None, help='主材单价列，如 I')
    ap.add_argument('--qty-col', default=None, help='工程量列，如 F')
    ap.add_argument('--note-col', default=None, help='负偏离/说明列，如 M')
    ap.add_argument('--min-row', type=int, default=None, help='数据起始行，默认自动跳过前 4 行表头')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()

    sheet, styles, wb, sheet_file, sheet_names = read_sheet(a.xlsx, a.sheet)
    fatal, warn, info = [], [], []

    # ---------- 1. 样式索引越界 ----------
    cx = re.search(r'<cellXfs[^>]*count="(\d+)"', styles)
    cx_count = int(cx.group(1)) if cx else 0
    real_count = len(re.findall(r'<xf [^>]*[^/]>.*?</xf>|<xf [^>]*/>',
                                (re.search(r'<cellXfs[^>]*>(.*?)</cellXfs>', styles, re.S) or ['', ''])[1], re.S))
    if cx_count != real_count:
        info.append('cellXfs 声明 count=%d，实际 %d 条（WPS 写出来的文件常不一致，按实际为准）'
                    % (cx_count, real_count))
    styles_used = [int(v) for v in re.findall(r's="(\d+)"', sheet)]
    if styles_used and max(styles_used) >= max(real_count, cx_count):
        fatal.append('样式索引越界：用到 s=%d，但 cellXfs 只有 %d 条' % (max(styles_used), max(real_count, cx_count)))

    # ---------- 2. 逐行结构 ----------
    rows = {}
    dup_rows, disorder_rows = [], []
    for m in ROW_RE.finditer(sheet):
        rn = int(m.group(1))
        cols = re.findall(r'<c r="([A-Z]+)%d"' % rn, m.group(3))
        rows[rn] = (cols, m.group(3))
        if len(cols) != len(set(cols)):
            seen, dup = set(), []
            for c in cols:
                if c in seen:
                    dup.append(c)
                seen.add(c)
            dup_rows.append((rn, sorted(set(dup))))
        ords = [col_ord(c) for c in cols]
        if ords != sorted(ords):
            disorder_rows.append(rn)

    if dup_rows:
        fatal.append('同一行出现重复单元格 %d 行 —— WPS/Excel 会取**第一个**（往往是空的那个），'
                     'openpyxl 取最后一个，所以自查看不出来。例：%s'
                     % (len(dup_rows), '；'.join('行%d 重复[%s]' % (r, ','.join(c)) for r, c in dup_rows[:5])))
    if disorder_rows:
        fatal.append('单元格列顺序非升序 %d 行（Excel 可能整行不显示或修复时丢弃内容）。例：%s'
                     % (len(disorder_rows), ','.join(map(str, disorder_rows[:8]))))

    # ---------- 3. 公式无缓存值 ----------
    no_cache = [int(r) for r, (c, b) in rows.items() if '<f' in b and '<v>' not in b]
    calc_ok = 'fullCalcOnLoad' in wb
    if no_cache and not calc_ok:
        warn.append('有 %d 行公式没有缓存值，且 workbook.xml 没开 fullCalcOnLoad —— '
                    '不重算的查看器（部分预览器/手机端）会显示空白或 0' % len(no_cache))
    elif no_cache:
        info.append('有 %d 行公式无缓存值，但已开 fullCalcOnLoad，打开会自动算' % len(no_cache))

    # ---------- 4. 配对检查（可选） ----------
    if a.model_col or a.price_col or a.note_col:
        start = a.min_row or 5
        maxrow = max(rows) if rows else 0
        mc, pc, qc, nc, pc2 = a.model_col, a.price_col, a.qty_col, a.note_col, a.param_col

        def get(rn, col):
            if not col:
                return None
            b = rows.get(rn, (None, ''))[1]
            m = re.search(r'<c r="%s%d"([^>]*?)(?:/>|>(.*?)</c>)' % (col, rn), b, re.S)
            if not m:
                return ''
            return cell_value(m.group(2))

        miss_price, miss_model, blank_both, pno_param = [], [], [], []
        for rn in range(start, maxrow + 1):
            if rn not in rows:
                continue
            model = (get(rn, mc) or '').strip()
            price = eval_simple(get(rn, pc))
            note = (get(rn, nc) or '').strip()
            param = (get(rn, pc2) or '').strip()
            # 章/节标题行（A 列无序号、也没有型号价格）不算数据行
            seq = (get(rn, 'A') or '').strip()
            if not seq and not model and price is None:
                continue
            if model and price is None:
                miss_price.append(rn)
            if price is not None and not model:
                miss_model.append(rn)
            if not model and not note and (mc and nc):
                blank_both.append(rn)
            if model and pc2 and not param:
                pno_param.append(rn)
        if miss_price:
            fatal.append('有型号但没有单价 %d 行：%s' % (len(miss_price), miss_price[:10]))
        if miss_model:
            fatal.append('有单价但没有型号 %d 行：%s' % (len(miss_model), miss_model[:10]))
        if blank_both:
            warn.append('既没型号也没说明（用户会认为「漏填」）%d 行：%s' % (len(blank_both), blank_both[:10]))
        if pno_param:
            warn.append('有型号但参数列为空 %d 行：%s' % (len(pno_param), pno_param[:10]))
        # 型号列 ASCII 合规（用户 2026-10-27 硬规则：型号列不许出现汉字/全角符号）
        if mc:
            nonascii = []
            for rn in range(start, maxrow + 1):
                if rn not in rows:
                    continue
                mv = (get(rn, mc) or '').strip()
                if mv and re.search(r'[^\x00-\x7f]', mv):
                    nonascii.append((rn, mv[:30]))
            if nonascii:
                warn.append('型号列含非 ASCII（汉字/全角 ＋ × 等）%d 行，按用户口径应清成纯型号、'
                            '把「套装/易展版/（单门）」等中文移到说明列。例：%s'
                            % (len(nonascii), '；'.join('行%d %s' % x for x in nonascii[:5])))

    # ---------- 5. 工程量是公式文本 ----------
    if a.qty_col and a.price_col:
        start = a.min_row or 5
        for rn in range(start, (max(rows) if rows else 0) + 1):
            if rn not in rows:
                continue
            b = rows[rn][1]
            mq = re.search(r'<c r="%s%d"([^>]*?)(?:/>|>(.*?)</c>)' % (a.qty_col, rn), b, re.S)
            mp = re.search(r'<c r="%s%d"([^>]*?)(?:/>|>(.*?)</c>)' % (a.price_col, rn), b, re.S)
            if not mq or not mp:
                continue
            qv = cell_value(mq.group(2))
            pv = eval_simple(cell_value(mp.group(2)))
            if pv is not None and isinstance(qv, str) and qv.strip().startswith('='):
                info.append('行%d 工程量是公式 %s（Python 直接累加会把这一行漏掉，报总额时记得按求得的值算）'
                            % (rn, qv.strip()))

    # ---------- 输出 ----------
    report = {
        'file': a.xlsx, 'sheet_file': sheet_file, 'sheets': sheet_names,
        'rows': len(rows), 'fatal': fatal, 'warn': warn, 'info': info,
        'ok': not fatal,
    }
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print('体检：%s（%s，%d 行）' % (a.xlsx, sheet_file, len(rows)))
        for tag, items in (('FATAL', fatal), ('WARN', warn), ('INFO', info)):
            for it in items:
                print('  [%s] %s' % (tag, it))
        print('结论：' + ('✅ 通过' if not fatal else '❌ 不许交付（%d 项致命）' % len(fatal)))
    return 0 if not fatal else 2


if __name__ == '__main__':
    sys.exit(main())
