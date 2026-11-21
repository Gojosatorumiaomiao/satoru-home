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
import unicodedata
import datetime

WS = "/home/hyr/.openclaw/workspace"
SITE = os.path.join(WS, "sites", "satoru-home")
SRC_STORIES = os.path.join(WS, "stories")
DAILY_JSON = os.path.join(WS, "data", "satoru-daily.json")
OUT_STORIES = os.path.join(SITE, "stories")

# 公开文本禁词表：真实词表只留本机，不进仓库。
# 可用环境变量 SATORU_BLOCKLIST 覆盖路径（测试用）。
BLOCKLIST_FILE = os.path.join(WS, "data", "public-blocklist.txt")

# 页面中需要替换的区块，用注释锚点标记
A_START = "<!-- stories:list:start -->"
A_END = "<!-- stories:list:end -->"
D_START = "<!-- daily:list:start -->"
D_END = "<!-- daily:list:end -->"

# 归档文件名：<date>.md / <date>-2.md / <date>-3.md（一天可有多篇）
ARCHIVE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.md$")

# 时刻格式：HH:MM（24 小时制）。动态条目的锚点 = 日期 + 时刻
# （entry_id 去掉冒号），因此缺失/空白/非法时刻会直接产出坏锚点
# （如 daily-2026-09-21-）并把空串写进发布账本，必须在校验阶段跳过。
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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


# 匹配前先去掉的零宽 / 双向控制字符（规范化副本专用，不影响发布正文）。
_SCAN_DROP_CHARS = (
    "\u200b"  # zero width space
    "\u200c"  # zero width non-joiner
    "\u200d"  # zero width joiner
    "\u2060"  # word joiner
    "\ufeff"  # zero width no-break space
    "\u180e"  # mongolian vowel separator
    "\u00ad"  # soft hyphen
    "\u200e\u200f"      # LRM / RLM
    "\u202a\u202b\u202c\u202d\u202e"  # 双向嵌入/覆盖
    "\u2066\u2067\u2068\u2069"        # 双向隔离
)
# 保留的空白与换行（不当作控制字符删除）
_SCAN_KEEP_WS = "\t\n\r"

# 通用类型检测规则（只含通用模式，不含任何真实姓名/单位/地址/行程/样本）。
# 顺序即优先级；命中的 reason 只给类别，不回显原文。
_TYPE_RULES = (
    # 邮箱
    ("email", re.compile(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    # 手机号（中国大陆 11 位，1 开头，第二位 3-9）
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 带分隔的固话/座机（区号-号码，或号码带连字符）
    ("phone", re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")),
    # 凭据格式：常见密钥前缀
    ("credential", re.compile(
        r"(?i)\b(?:sk|pk|ghp|gho|ghu|ghs|github_pat|AKIA|ASIA|xox[baprs])"
        r"[_\-][A-Za-z0-9_\-]{12,}")),
    # 凭据格式：Bearer / token / key 等键值，或 Bearer 后跟长串
    ("credential", re.compile(
        r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("credential", re.compile(
        r"(?i)\b(?:token|api[_\-]?key|secret|password|passwd|pwd)"
        r"\s*[:=]\s*\S{6,}")),
    # 本机绝对路径（家目录 / 系统目录）
    ("local-path", re.compile(
        r"(?:/home/[A-Za-z0-9._\-]+|/Users/[A-Za-z0-9._\-]+|/root"
        r"|[A-Za-z]:\\Users\\[A-Za-z0-9._\-]+)")),
    # WSL 下访问 Windows 侧家目录的写法：/mnt/<盘符>/Users/<用户>/…
    # 说明：规范写法 /mnt/c/Users/<name> 其实已被上一条的 /Users/<name>
    # 子串命中；真正漏掉的是大小写变体（Windows 文件系统不区分大小写，
    # 如 /mnt/c/users/<name>）。本规则显式、且忽略大小写。
    ("local-path", re.compile(
        r"/mnt/[A-Za-z]/Users/[A-Za-z0-9._\-]+", re.IGNORECASE)),
    # IPv4：只在**有网络语境**时拦，避免把裸写的版本号 1.2.3.4
    # 一类正常文案误判（用户要求控制误报）。要求前面出现
    # IP/地址/服务器/端口/host 等提示词，或值出现在 URL 中。
    ("ip-address", re.compile(
        r"(?i)(?:ip|地址|服务器|主机|端口|host|server|address)\s*[:：=\s]\s*"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|[1-9]|0)"
        r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|[1-9]|0)){3}"
        r"(?![A-Za-z0-9.])")),
    ("ip-address", re.compile(
        r"(?<![A-Za-z0-9.])"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|[1-9]|0)"
        r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|[1-9]|0)){3}"
        r"(?:(?::\d{1,5})|(?:/\d{1,2}))(?![A-Za-z0-9.])")),
)

# 已定义的内部标记模式（**不粗暴拦截所有【……】**，只认这些前缀），
# 避免误伤「【今日推荐】」这类正常括号文案。
_INTERNAL_MARKER_RE = re.compile(
    r"【\s*(?:内部|私人|私密|仅内部|勿公开|请勿公开|do\s*not\s*publish"
    r"|internal|private)\s*[^】]{0,40}】",
    re.IGNORECASE,
)


def normalize_for_scan(text):
    """生成**只用于禁词匹配**的规范化副本。

    1. NFKC：把全角/兼容字符折成基本形式（ＳＥＣＲＥＴ -> SECRET）；
    2. 去掉零宽与双向控制字符（含 U+200B 等），
       避免「绝<U+200B>密代号」这类可见相同的写法绕过子串匹配。

    发布正文一律使用原文，**不用**这个副本改写公开内容。
    只做匹配，不回显命中的禁词或命中位置。
    """
    out = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        if ch in _SCAN_DROP_CHARS:
            continue
        if ch not in _SCAN_KEEP_WS and unicodedata.category(ch) in ("Cc", "Cf", "Cs"):
            continue
        out.append(ch)
    return "".join(out)


def scan_public_text(text):
    """扫描待公开文本。返回 (ok, reason)。

    禁词表是本机文件，不进仓库（见 docs：真实禁词表只留本机）。
    读取顺序：环境变量 SATORU_BLOCKLIST 指定的路径；否则
    workspace/data/public-blocklist.txt（本机、已 gitignore）。
    两个位置都没有时 **不是“通过”** —— 扫描无法进行，
    返回 ok=False 让调用方跳过并报告，不静默放行。

    匹配前对**副本**做 NFKC 规范化并去掉零宽/控制字符（见
    normalize_for_scan），因此全角或插入零宽的相同写法同样会被拦下；
    发布正文仍保留 public_text 原文，不做任何改写。
    返回的 reason 只给类别（blocked / email / phone / ... / no-blocklist），
    **不回显命中的原文、真实禁词或位置**。

    两层检测：
      1. 本机禁词子串（真实人名/单位/地址/行程/私人词，只在本机词表）；
      2. 通用类型规则 _TYPE_RULES + 内部标记模式 _INTERNAL_MARKER_RE
         —— 邮箱、手机号、凭据、本机绝对路径、IP。这些是通用模式，
         不含任何真实样本，可以公开。

    范围说明：当前仅被日常动态条目调用（daily_entries_for -> sync_daily）；
    故事归档路径不经过本函数。
    """
    path = os.environ.get("SATORU_BLOCKLIST") or BLOCKLIST_FILE
    if not os.path.isfile(path):
        return False, "no-blocklist"
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception as exc:
        return False, "blocklist-error:%s" % exc.__class__.__name__
    terms = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        n = normalize_for_scan(s)
        if n:
            terms.append(n)
    if not terms:
        return False, "blocklist-empty"

    # 所有检测都跑在规范化副本上；发布正文始终是 public_text 原文。
    probe = normalize_for_scan(text)

    for term in terms:
        if term.lower() in probe.lower():
            return False, "blocked"

    for reason, rx in _TYPE_RULES:
        if rx.search(probe):
            return False, reason

    if _INTERNAL_MARKER_RE.search(probe):
        return False, "internal-marker"

    return True, "ok"


def valid_time(t):
    """时刻必须是 HH:MM（24 小时制），否则无法生成合法锚点。

    缺失（键不存在）、空白串与非法写法（如 "25:99"、"9:00 " 之外的
    "9"、"9.00"）一律判为无效。main 也在此处过滤，本 PR 的闸门改写
    曾把这一步丢掉，属于回归。
    """
    return bool(_TIME_RE.match(t))


def daily_entries_for(state, date):
    """取当天可发布的动态条目，按时间排序。

    每条返回 (time, text) 或 (time, None) —— 后者表示该条被闸门拦下。

    闸门规则（用户指定）：明确公开的 public_text → 扫描 → 发布。
      - 时刻缺失/空白/非法（非 HH:MM）        -> 跳过并报告 invalid-time
      - 没有 public_text（只有内部 summary）  -> 跳过并报告
      - 扫描命中禁词                        -> 跳过并报告
      - 禁词表缺失/不可读/为空（无法扫描）    -> 跳过并报告
    被跳过的条目 **不消耗发布名额**，也不影响已有页面；
    返回的 reason 只给类别，不回显正文。
    """
    if not state:
        return [], []
    contacts = state.get("contacts") or []
    out, skipped = [], []
    for c in contacts:
        if c.get("kind") != "normal":
            continue
        t = (c.get("time") or "").strip()
        # 先校验时刻：坏时刻即使有合格 public_text 也会生成坏锚点/坏账本
        if not valid_time(t):
            skipped.append({"time": t, "reason": "invalid-time"})
            continue
        public = (c.get("public_text") or "").strip()
        if not public:
            skipped.append({"time": t, "reason": "no-public-text"})
            continue
        ok, why = scan_public_text(public)
        if not ok:
            skipped.append({"time": t, "reason": why})
            continue
        out.append((t, public))

    out.sort(key=lambda x: x[0])
    return out, skipped


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
    entries, _skipped = daily_entries_for(state, date)
    if not entries:
        return None
    return "\n".join(render_daily_entry(date, t, text) for t, text in entries)


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
    返回 (action, added, updated, skipped)。

    所有返回路径都必须给出四项，否则调用方在生成报告前就会解包失败：
    早退（no-entry / no-anchor）用空列表占位第四项。

    skipped 是被闸门拦下的条目（缺 public_text / 命中禁词 / 无法扫描）。
    它们**不消耗发布名额**：发布游标只按已记账的条目推进，
    不被跳过项占位；当天页面上已有内容也不受影响。
    """
    all_entries, skipped = daily_entries_for(state, date)
    if not all_entries:
        return "no-entry", 0, 0, skipped

    s = open(path, encoding="utf-8").read()
    i = s.find(D_START)
    j = s.find(D_END)
    if i == -1 or j == -1:
        return "no-anchor", 0, 0, skipped

    log = load_published_log()
    # 记账是唯一真相：某时刻一旦记过，就不再当作新条目。
    # 页面只作为内容载体，不用来反推“是否已发布”——
    # 否则手工调整页面后，脚本会把旧时刻当新条目补发。
    recorded = set(log.get(date, []))
    on_page = published_times(path, date)

    # 游标口径：用**稳定时刻**判断是否已发布，不用过滤后的数组下标。
    # 过滤结果会随 public_text 补齐 / 禁词表变化而改变，
    # 若拿「过滤后下标 == 记账长度」配对，早时段条目被过滤过一次之后
    # 下标永远对不上，就再也发不出去（复核已复现）。
    # 改为：从「合格且未记账」的条目里按时间取最早一条。
    # 已记过的不再当新条目，已撤下的记录也不复活。
    todo = []
    newly = []
    for t, text in all_entries:
        if t in recorded:
            # 已发布过：页面还在就原位刷新，不在就不复活
            if t in on_page:
                todo.append((t, text))
    # 每次最多新增一条（all_entries 已按时间排序）；每天不超过 max_per_day 条
    pending = [(t, text) for t, text in all_entries if t not in recorded]
    if pending and len(recorded) < max_per_day:
        t, text = pending[0]
        todo.append((t, text))
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
    return "ok", added, updated, skipped


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
        action, added, updated, skipped = sync_daily(daily_html, date, state)
        if action == "no-anchor":
            report["steps"].append("daily.html 缺少锚点，未更新")
        elif action == "no-entry":
            report["steps"].append("当天没有可发布的公共成稿")
        else:
            report["steps"].append(
                "daily.html 已同步：新增 %d 条，更新 %d 条" % (added, updated))
            report["daily_added"] = added
            report["daily_updated"] = updated
            if added or updated:
                report["changed"] = True
            else:
                report["steps"].append("本时段已发布，跳过")

        # 闸门拦下的条目：跳过并报告，不消耗名额、不动已有页面
        if skipped:
            report["skipped"] = skipped
            for item in skipped:
                report["steps"].append(
                    "跳过 %s：%s" % (item["time"] or "(无时刻)", item["reason"]))

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
