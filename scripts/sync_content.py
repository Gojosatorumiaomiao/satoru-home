#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已授权的公开虚构内容同步到 satoru-home 网站。

读取（只读）：
  /home/hyr/.openclaw/workspace/stories/YYYY-MM-DD.md  当日故事全文
  /home/hyr/.openclaw/workspace/stories/INDEX.md       标题索引
  /home/hyr/.openclaw/workspace/data/satoru-daily.json 当日虚构生活状态

写入：
  sites/satoru-home/stories/YYYY-MM-DD.md   故事归档（公开可读）
  sites/satoru-home/stories.html            故事列表页（保留全部历史）
  sites/satoru-home/daily.html              日常动态页（追加当日一条）

设计约束：
  - 只发布故事正文与适合公开的动态；不上传私人记忆、聊天记录或原始状态文件。
  - 同一天不重复生成、不重复发布（幂等）。
  - 不改网站设计，只替换内容区块。

用法：
  python3 sync_content.py [日期]              故事 + 日常动态（22:00 用）
  python3 sync_content.py [日期] --daily-only 只更新日常动态，不碰故事页
"""
import json
import os
import re
import sys
import datetime

WS = "/home/hyr/.openclaw/workspace"
SITE = os.path.join(WS, "sites", "satoru-home")
SRC_STORIES = os.path.join(WS, "stories")
DAILY_JSON = os.path.join(WS, "data", "satoru-daily.json")
OUT_STORIES = os.path.join(SITE, "stories")

# 页面中需要替换的区块，用注释锚点标记
A_START = "<!-- stories:list:start -->"
A_END = "<!-- stories:list:end -->"
D_START = "<!-- daily:list:start -->"
D_END = "<!-- daily:list:end -->"

# 归档文件名：<date>.md / <date>-2.md / <date>-3.md（一天可有多篇）
ARCHIVE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.md$")


def html_escape(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def strip_date_prefix(title):
    """"# 2026-09-15 三分钟的空白" -> "三分钟的空白"；不含日期则原样返回。"""
    m = re.match(r"^\d{4}-\d{2}-\d{2}\s+(.*)$", title)
    return m.group(1).strip() if m else title


def read_source_stories(date):
    """读取当天源文件中的全部故事。返回 [(title, paragraphs), ...]。

    一个日期文件可能含多篇（如 2026-09-12.md 有三篇），
    每篇以一行 "# 标题" 开头。不能只取第一篇。
    """
    p = os.path.join(SRC_STORIES, date + ".md")
    if not os.path.exists(p):
        return []
    raw = open(p, encoding="utf-8").read().strip()

    chunks = []
    cur_title = None
    cur_lines = []
    for ln in raw.split("\n"):
        if ln.startswith("# "):
            if cur_title is not None:
                chunks.append((cur_title, cur_lines))
            cur_title = strip_date_prefix(ln[2:].strip())
            cur_lines = []
        elif cur_title is not None:
            cur_lines.append(ln)
    if cur_title is not None:
        chunks.append((cur_title, cur_lines))

    out = []
    for title, lines in chunks:
        paras = [x.strip() for x in "\n".join(lines).split("\n\n") if x.strip()]
        if paras:
            out.append((title, paras))
    return out


def read_source_story(date):
    """兼容旧调用：返回当天第一篇 (title, paragraphs) 或 None。"""
    stories = read_source_stories(date)
    return stories[0] if stories else None


def read_index():
    """读取 INDEX.md 的日期->摘要。"""
    out = {}
    p = os.path.join(SRC_STORIES, "INDEX.md")
    if not os.path.exists(p):
        return out
    for ln in open(p, encoding="utf-8"):
        m = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", ln)
        if m:
            out.setdefault(m.group(1), []).append((m.group(2), m.group(3)))
    return out


def read_daily_state():
    """读取当日虚构生活状态（只取用于生成公开动态的部分）。"""
    if not os.path.exists(DAILY_JSON):
        return None
    try:
        return json.load(open(DAILY_JSON, encoding="utf-8"))
    except Exception:
        return None


def daily_state_for(state, date):
    """校验状态文件属于目标日期，拒绝把昨日状态当今日内容。

    返回 (state, error)。state 为 None 时 error 说明原因。
    """
    if not state:
        return None, "状态文件不存在或无法解析"
    state_date = (state.get("date") or "").strip()
    if not state_date:
        return None, "状态文件缺少 date 字段，无法确认归属日期"
    if state_date != date:
        return None, ("状态文件日期为 %s，与目标日期 %s 不符，"
                      "拒绝发布避免把往日内容当作今日" % (state_date, date))
    return state, None


def daily_entries_for(state, date):
    """取当天所有适合公开的动态条目，按时间排序。

    每条返回 (time, text, screened)。一天可有多条；用 time 作为条目锚点。

    screened 表示该条是否来自明确标记为"可公开"的来源：
    优先取 contact["public_text"]；没有则退回 contact["summary"]，
    此时 screened=False —— 行为不变，但调用方应把它计入未筛选数量并报警。
    """
    if not state:
        return []
    contacts = state.get("contacts") or []
    out = []
    for c in contacts:
        if c.get("kind") != "normal":
            continue
        t = (c.get("time") or "").strip()
        public = (c.get("public_text") or "").strip()
        if public:
            text, screened = public, True
        else:
            text, screened = (c.get("summary") or "").strip(), False
        if not t or not text:
            continue
        out.append((t, text, screened))
    out.sort(key=lambda x: x[0])
    return out


def story_slug(date, title, index):
    """归档文件名：当天一篇用 <date>.md，多篇用 <date>-<n>.md。"""
    return "%s.md" % date if index == 0 else "%s-%d.md" % (date, index + 1)


def write_story_archive(date, title, paras, filename=None):
    """写出单篇归档，保留原标题与完整正文。

    已存在的归档是**已公开原文**，任何情况下都不覆盖：
      - 内容一致  -> "reused"，不重写文件
      - 内容不同  -> "conflict"，保留已有原文，把差异交回调用方报告
    返回 (path, status, archive_paras)；archive_paras 仅在冲突时给出旧文件段数。
    """
    os.makedirs(OUT_STORIES, exist_ok=True)
    p = os.path.join(OUT_STORIES, filename or (date + ".md"))
    body = "\n\n".join(paras)
    content = "# %s %s\n\n%s\n" % (date, title, body)
    if os.path.exists(p):
        old = open(p, encoding="utf-8").read()
        if old == content:
            return p, "reused", None
        old_lines = old.strip().split("\n")
        old_paras = [x for x in "\n".join(old_lines[1:]).split("\n\n") if x.strip()]
        return p, "conflict", len(old_paras)
    open(p, "w", encoding="utf-8").write(content)
    return p, "written", None


def render_story_list():
    """扫描 stories/ 下全部归档，按日期（同日按序号）倒序渲染。

    文件名约定：一天一篇用 <date>.md，多篇用 <date>-2.md、<date>-3.md。
    首段整段显示在折叠之外，其余段落进入 details；
    折叠之外 + 展开区 = 原文全段，不漏字、不重字。
    """
    if not os.path.isdir(OUT_STORIES):
        return ""

    items = []
    for f in os.listdir(OUT_STORIES):
        m = ARCHIVE_RE.match(f)
        if m:
            items.append((m.group(1), int(m.group(2) or 1), f))
    # 日期倒序；同日按序号倒序
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)

    blocks = []
    for date, seq, f in items:
        raw = open(os.path.join(OUT_STORIES, f), encoding="utf-8").read().strip()
        lines = raw.split("\n")
        head = lines[0] if lines else ""
        title = strip_date_prefix(re.sub(r"^#\s*", "", head).strip())
        paras = [p.strip() for p in "\n".join(lines[1:]).split("\n\n") if p.strip()]
        lead = paras[0] if paras else ""
        rest = paras[1:]
        more = ""
        if rest:
            more = "\n        <details>\n          <summary>继续读</summary>\n" + \
                   "\n".join("          <p>%s</p>" % html_escape(x) for x in rest) + \
                   "\n        </details>"
        blocks.append(
            '      <article class="story" id="story-%s">\n'
            '        <div class="story-meta">%s</div>\n'
            '        <h3>%s</h3>\n'
            '        <p>%s</p>%s\n'
            '        <p class="muted"><a class="story-link" href="stories/%s">全文</a></p>\n'
            '      </article>' % (f[:-3], date, html_escape(title), html_escape(lead), more, f)
        )
    return "\n\n".join(blocks)


def entry_id(date, t):
    """条目锚点：日期 + 时刻（去冒号）。同一天不同时刻各占一条。"""
    return "daily-%s-%s" % (date, t.replace(":", ""))


def render_daily_entry(date, t, text):
    """渲染单条日常动态。"""
    return ('      <article class="post" id="%s">\n'
            '        <time>%s · %s</time>\n'
            '        <p>%s</p>\n'
            '      </article>' % (entry_id(date, t), date, t, html_escape(text)))


def render_daily_list(state, date):
    """渲染当天全部动态条目。同一天多条分别保留。"""
    entries = daily_entries_for(state, date)
    if not entries:
        return None
    return "\n".join(render_daily_entry(date, t, text)
                     for t, text, _screened in entries)


MAX_PER_DAY = 6      # 每天最多发布的动态条数
PUBLISHED_LOG = os.path.join(SITE, "data", "daily-published.json")


def load_published_log():
    """已发布动态的记账文件：{日期: [时刻, ...]}。

    用它而不是页面内容来判断“是否已发布”，避免条目被手工调整后
    脚本又把旧时刻当作新条目补发。
    """
    if not os.path.exists(PUBLISHED_LOG):
        return {}
    try:
        return json.load(open(PUBLISHED_LOG, encoding="utf-8"))
    except Exception:
        return {}


def save_published_log(log):
    os.makedirs(os.path.dirname(PUBLISHED_LOG), exist_ok=True)
    json.dump(log, open(PUBLISHED_LOG, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)


def published_times(path, date):
    """读取当天页面上已发布的时刻集合（兼容新旧锚点）。"""
    s = open(path, encoding="utf-8").read()
    times = set()
    for m in re.finditer(r'<article class="post" id="daily-%s(?:-(\d{4}))?"' % re.escape(date), s):
        if m.group(1):
            times.add("%s:%s" % (m.group(1)[:2], m.group(1)[2:]))
        else:
            t = re.search(r'<time>\s*%s\s*·\s*(\d{2}:\d{2})' % re.escape(date), s)
            if t:
                times.add(t.group(1))
    return times


def sync_daily(path, date, state, max_new=1, max_per_day=MAX_PER_DAY):
    """把当天动态幂等写入 daily.html。

    - 每次最多新增 max_new 条（默认 1），避免把内部状态记录批量发布。
    - 每天最多 max_per_day 条。
    - 同一时刻重跑时原位更新，不新增重复条目。
    - 不删除其它日期的条目。
    - 已发布时刻记录在 daily-published.json，不依赖页面反推。
    返回 (action, added, updated, unscreened)。

    所有返回路径都必须给出四项，否则调用方在生成报告前就会解包失败：
    早退（no-entry / no-anchor）用空列表占位第四项。
    """
    all_entries = daily_entries_for(state, date)
    if not all_entries:
        return "no-entry", 0, 0, []

    s = open(path, encoding="utf-8").read()
    i = s.find(D_START)
    j = s.find(D_END)
    if i == -1 or j == -1:
        return "no-anchor", 0, 0, []

    log = load_published_log()
    # 记账是唯一真相：某时刻一旦记过，就不再当作新条目。
    # 页面只作为内容载体，不用来反推“是否已发布”——
    # 否则手工调整页面后，脚本会把旧时刻当新条目补发。
    recorded = set(log.get(date, []))
    on_page = published_times(path, date)
    day_total = len(recorded | on_page)

    # C 方案：行为不变，但统计有多少条来自未筛选的内部摘要，
    # 由调用方在报告里计数并报警，不静默当作已授权公开文案。
    unscreened = [t for t, _text, screened in all_entries if not screened]

    # 待写入：记账里已有的只做原位更新；新时刻受 max_new 与 max_per_day 限制
    todo = []
    newly = []
    pending_new = 0
    planned_total = day_total
    for t, text, _screened in all_entries:
        if t in recorded:
            # 已发布过：页面还在就原位刷新，不在就不复活
            if t in on_page:
                todo.append((t, text))
            continue
        if planned_total >= max_per_day:
            continue
        if pending_new >= max_new:
            continue
        todo.append((t, text))
        pending_new += 1
        planned_total += 1
        newly.append(t)

    head, body, tail = s[:i + len(D_START)], s[i + len(D_START):j], s[j:]

    # 收集已有条目：按锚点拆块，保留其它日期的原样
    existing = {}
    other_blocks = []
    for m in re.finditer(r'<article class="post" id="([^"]+)">.*?</article>',
                         body, re.S):
        block, aid = m.group(0), m.group(1)
        # 兼容两种锚点：新的 daily-<date>-<HHMM> 与旧的 daily-<date>
        if aid == "daily-%s" % date or aid.startswith("daily-%s-" % date):
            # 从锚点取出时刻，便于与源数据比对
            existing[aid] = block
        else:
            other_blocks.append(block)

    added = updated = 0
    day_blocks = []
    for t, text in todo:
        aid = entry_id(date, t)
        new_block = render_daily_entry(date, t, text)
        if aid in existing:
            if existing[aid].strip() != new_block.strip():
                updated += 1
            day_blocks.append(new_block)
        else:
            day_blocks.append(new_block)
            added += 1
        existing.pop(aid, None)

    # 当天已存在但源数据里没有的条目（如状态被回滚）保留，避免静默删内容
    for aid, block in existing.items():
        day_blocks.append(block)

    # 当天条目按时间排序置于顶部，其余日期条目保持原有顺序在后
    day_blocks.sort(key=lambda b: re.search(r'<time>([^<]+)</time>', b).group(1))
    newbody = "\n" + "\n".join(day_blocks)
    if other_blocks:
        newbody += "\n" + "\n".join(other_blocks)
    newbody += "\n"

    open(path, "w", encoding="utf-8").write(head + newbody + "    " + tail)

    if newly:
        log.setdefault(date, [])
        for t in newly:
            if t not in log[date]:
                log[date].append(t)
        log[date].sort()
        save_published_log(log)
    return "ok", added, updated, unscreened


def replace_block(path, start, end, inner):
    s = open(path, encoding="utf-8").read()
    i = s.find(start)
    j = s.find(end)
    if i == -1 or j == -1:
        return False
    s = s[:i + len(start)] + "\n" + inner + "\n    " + s[j:]
    open(path, "w", encoding="utf-8").write(s)
    return True


L_START = "<!-- latest-story:start -->"
L_END = "<!-- latest-story:end -->"


def render_latest_story():
    """首页最新故事入口：取归档中最新一篇，给标题、日期和阅读入口。

    只引用已有归档，不复制正文，避免首页与归档不一致。
    """
    if not os.path.isdir(OUT_STORIES):
        return ""
    items = []
    for f in os.listdir(OUT_STORIES):
        m = ARCHIVE_RE.match(f)
        if m:
            items.append((m.group(1), int(m.group(2) or 1), f))
    if not items:
        return ""
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    date, seq, f = items[0]

    raw = open(os.path.join(OUT_STORIES, f), encoding="utf-8").read().strip()
    lines = raw.split("\n")
    title = strip_date_prefix(re.sub(r"^#\s*", "", lines[0] if lines else "").strip())
    paras = [p.strip() for p in "\n".join(lines[1:]).split("\n\n") if p.strip()]
    lead = paras[0] if paras else ""

    return (
        '    <article class="story" id="latest-story">\n'
        '      <div class="story-meta">%s</div>\n'
        '      <h3>%s</h3>\n'
        '      <p>%s</p>\n'
        '      <p class="muted">'
        '<a class="story-link" href="stories.html#story-%s">在故事页读全文</a>'
        '</p>\n'
        '    </article>' % (date, html_escape(title), html_escape(lead), f[:-3])
    )


def main():
    args = [a for a in sys.argv[1:]]
    daily_only = "--daily-only" in args
    args = [a for a in args if not a.startswith("--")]
    date = args[0] if args else datetime.date.today().isoformat()
    report = {"date": date, "mode": "daily-only" if daily_only else "full",
              "steps": [], "changed": False}

    # ---------- 故事（--daily-only 时完全跳过，不碰 stories.html 与归档） ----------
    if daily_only:
        report["steps"].append("daily-only：跳过故事处理，不修改故事页")
    else:
        # 支持一天多篇：把源文件里的每一篇都归档，一篇都不能丢
        stories = read_source_stories(date)
        if not stories:
            report["steps"].append("未找到当天故事源文件；继续处理日常动态")
        else:
            for idx, (title, paras) in enumerate(stories):
                fname = story_slug(date, title, idx)
                _, status, old_paras = write_story_archive(date, title, paras, fname)
                if status == "written":
                    report["steps"].append(
                        "写入归档：stories/%s（《%s》%d 段）" % (fname, title, len(paras)))
                elif status == "reused":
                    report["steps"].append(
                        "复用已有归档（内容一致）：stories/%s（《%s》%d 段）"
                        % (fname, title, len(paras)))
                else:
                    # 已公开原文优先：只报告差异，不改写已发布内容
                    report["steps"].append(
                        "保留已有归档，未覆盖：stories/%s（源文 %d 段 / 已归档 %d 段；"
                        "《%s》）" % (fname, len(paras), old_paras or 0, title))
                    report.setdefault("archive_conflicts", []).append(
                        {"file": fname, "title": title,
                         "source_paras": len(paras), "archive_paras": old_paras or 0})
            report["stories"] = [t for t, _ in stories]

            stories_html = os.path.join(SITE, "stories.html")
            if replace_block(stories_html, A_START, A_END, render_story_list()):
                report["steps"].append("更新 stories.html 列表")
                report["changed"] = True
            else:
                report["steps"].append("stories.html 缺少锚点，未更新")

        # 首页最新故事入口（引用归档，不复制正文）
        index_html = os.path.join(SITE, "index.html")
        if replace_block(index_html, L_START, L_END, render_latest_story()):
            report["steps"].append("更新 index.html 最新故事入口")
            report["changed"] = True
        else:
            report["steps"].append("index.html 缺少最新故事锚点，未更新")

    # ---------- 日常动态（每次都做） ----------
    state, err = daily_state_for(read_daily_state(), date)
    if err:
        report["steps"].append("日常动态未更新：" + err)
        report["daily_error"] = err
    else:
        daily_html = os.path.join(SITE, "daily.html")
        action, added, updated, unscreened = sync_daily(daily_html, date, state)
        if action == "no-anchor":
            report["steps"].append("daily.html 缺少锚点，未更新")
        elif action == "no-entry":
            report["steps"].append("当天没有可公开的动态条目")
        else:
            report["steps"].append(
                "daily.html 已同步：新增 %d 条，更新 %d 条" % (added, updated))
            report["daily_added"] = added
            report["daily_updated"] = updated
            # C 方案：不阻断发布，但把未筛选来源数量报出来
            if unscreened:
                report["unscreened_internal"] = len(unscreened)
                report["steps"].append(
                    "警告：%d 条来自内部摘要 summary（未标记 public_text），"
                    "未经公开筛选：%s" % (len(unscreened), ", ".join(unscreened)))
            if added or updated:
                report["changed"] = True
            else:
                report["steps"].append("本时段已发布，跳过")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
