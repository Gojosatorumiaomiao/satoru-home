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


def html_escape(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def read_source_story(date):
    """读取当天故事原文。返回 (title, body_paragraphs) 或 None。"""
    p = os.path.join(SRC_STORIES, date + ".md")
    if not os.path.exists(p):
        return None
    raw = open(p, encoding="utf-8").read().strip()
    lines = raw.split("\n")
    title = ""
    body_lines = []
    for ln in lines:
        if ln.startswith("# ") and not title:
            # "# 2026-09-15 三分钟的空白" -> "三分钟的空白"
            title = ln[2:].strip()
            m = re.match(r"^\d{4}-\d{2}-\d{2}\s+(.*)$", title)
            if m:
                title = m.group(1).strip()
        else:
            body_lines.append(ln)
    paras = [p.strip() for p in "\n".join(body_lines).split("\n\n") if p.strip()]
    return title, paras


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


def public_daily_line(state):
    """从虚构状态中取一条适合公开的动态文本。"""
    if not state:
        return None
    contacts = state.get("contacts") or []
    # 取当天最后一条 normal（有具体事件），没有就退回 thought
    pick = None
    for c in reversed(contacts):
        if c.get("kind") == "normal":
            pick = c
            break
    if pick is None and contacts:
        pick = contacts[-1]
    if pick is None:
        return None
    return pick.get("time", ""), pick.get("summary", "")


def write_story_archive(date, title, paras):
    os.makedirs(OUT_STORIES, exist_ok=True)
    p = os.path.join(OUT_STORIES, date + ".md")
    body = "\n\n".join(paras)
    open(p, "w", encoding="utf-8").write("# %s %s\n\n%s\n" % (date, title, body))
    return p


def render_story_list():
    """扫描 stories/ 下全部归档，按日期倒序渲染列表。"""
    if not os.path.isdir(OUT_STORIES):
        return ""
    files = sorted([f for f in os.listdir(OUT_STORIES)
                    if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", f)], reverse=True)
    blocks = []
    for f in files:
        date = f[:-3]
        raw = open(os.path.join(OUT_STORIES, f), encoding="utf-8").read().strip()
        lines = raw.split("\n")
        head = lines[0] if lines else ""
        title = re.sub(r"^#\s*\d{4}-\d{2}-\d{2}\s*", "", head).strip()
        body_lines = lines[1:]
        paras = [p.strip() for p in "\n".join(body_lines).split("\n\n") if p.strip()]
        # 首段整段显示在折叠之外；其余段落进入 details。
        # 不能截断首段：截断后剩余段落从 paras[1] 开始，
        # 首段被截去的尾部既不在摘要里也不在展开正文里，会静默丢字。
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
            '        <p class="muted"><a class="story-link" href="stories/%s.md">全文</a></p>\n'
            '      </article>' % (date, date, html_escape(title), html_escape(lead), more, date)
        )
    return "\n\n".join(blocks)


def render_daily_list(state):
    """渲染日常动态列表（当日一条，幂等）。"""
    line = public_daily_line(state)
    if not line:
        return None
    t, text = line
    date = state.get("date", "")
    return ('      <article class="post" id="daily-%s">\n'
            '        <time>%s · %s</time>\n'
            '        <p>%s</p>\n'
            '      </article>' % (date, date, t, html_escape(text)))


def replace_block(path, start, end, inner):
    s = open(path, encoding="utf-8").read()
    i = s.find(start)
    j = s.find(end)
    if i == -1 or j == -1:
        return False
    s = s[:i + len(start)] + "\n" + inner + "\n    " + s[j:]
    open(path, "w", encoding="utf-8").write(s)
    return True


def upsert_daily(path, date, block):
    """幂等写入当日动态：存在则原位替换，不存在则插到列表区顶部。"""
    s = open(path, encoding="utf-8").read()
    marker = 'id="daily-%s"' % date
    if marker in s:
        # 定位该条目所在 <article>，整段替换（同时清掉可能已存在的重复项）
        first = s.find(marker)
        start = s.rfind("<article", 0, first)
        end = s.find("</article>", first) + len("</article>")
        # 向前吃掉该行缩进
        line_start = s.rfind("\n", 0, start) + 1
        s = s[:line_start] + block.strip() + s[end:]
        # 清掉同一天的其它残留副本
        while s.count(marker) > 1:
            k = s.rfind(marker)
            a = s.rfind("<article", 0, k)
            b = s.find("</article>", k) + len("</article>")
            la = s.rfind("\n", 0, a) + 1
            s = s[:la] + s[b:]
            # 收拾可能留下的空行
            s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
        open(path, "w", encoding="utf-8").write(s)
        return "updated"
    k = s.find(D_START)
    if k == -1:
        return "no-anchor"
    s = s[:k + len(D_START)] + "\n" + block + s[k + len(D_START):]
    open(path, "w", encoding="utf-8").write(s)
    return "inserted"


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    report = {"date": date, "steps": [], "changed": False}

    src = read_source_story(date)
    if not src:
        report["steps"].append("未找到当天故事源文件，跳过")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    title, paras = src
    report["steps"].append("读取故事：%s（%d 段）" % (title, len(paras)))

    archive = os.path.join(OUT_STORIES, date + ".md")
    existed = os.path.exists(archive)
    write_story_archive(date, title, paras)
    report["steps"].append(("复用已有归档" if existed else "写入归档") + "：stories/%s.md" % date)

    stories_html = os.path.join(SITE, "stories.html")
    if replace_block(stories_html, A_START, A_END, render_story_list()):
        report["steps"].append("更新 stories.html 列表")
        report["changed"] = True
    else:
        report["steps"].append("stories.html 缺少锚点，未更新")

    state = read_daily_state()
    daily_block = render_daily_list(state) if state else None
    if daily_block:
        daily_html = os.path.join(SITE, "daily.html")
        action = upsert_daily(daily_html, date, daily_block)
        if action == "no-anchor":
            report["steps"].append("daily.html 缺少锚点，未更新")
        else:
            report["steps"].append("daily.html %s 当日条目（%s）" %
                                      ("更新" if action == "updated" else "追加", date))
            report["changed"] = True
    else:
        report["steps"].append("无可用当日状态，跳过日常动态")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
