#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布守卫：白名单路径没有真实变化时不创建提交。

背景
----
2026-10-08 20:00 出现两个同名连续提交 `658f3f3` 与 `fd22c1b`；后者与父提交的
tree 完全相同，是一个**空提交**，只制造历史噪声，并可能无意义地触发 Pages 构建。
本仓库的 `scripts/sync_content.py` 本身不创建提交（它只写工作区文件并打印
`"changed"` 报告），提交步骤由发布流程在 git 层直接完成，缺少「提交前的最终树比较」。

设计（按 Issue #3 复核 2026-10-09 早间 P0 收窄）
------------------------------------------------
1. **白名单必填**：`--path` 至少一项；`--preset daily` / `--preset story` 提供固定白名单。
   不再有「省略 paths 即扫描整棵工作树」的过宽默认。
2. **只看白名单、只比 HEAD**：先 `git add -A -- <白名单>`，再用
   `git diff --cached --quiet HEAD -- <白名单>` 判断暂存区相对当前 HEAD 是否有新 tree；
   无差异直接 no-change。**不再依赖 `origin/main` 或任何远端引用**
   （本机 fetch/push 走 https 会超时，远端默认不稳）。
3. **提交只覆盖白名单**：`git commit -m <msg> -- <白名单>`，工作区其他未跟踪文件或
   无关改动不会进入本次发布提交。
4. **dry-run 只读**：`--dry-run` 不调用 `git add`，只用 `git diff --name-only HEAD`
   与 `git ls-files --others --exclude-standard` 探测白名单内改动，**不改动索引**
   （按 Issue #3 复核 2026-10-09 午后 P0 修正）。
5. **「已提交、推送未确认」是独立状态**：正式路径先建 commit 再 `git push`，若推送失败，
   commit 仍留在 HEAD；绝不能让它落进后续的普通 `no-change` 而被永久滞留在本地。因此：
   - 推送失败 -> 保留 commit，把 `{commit, branch, remote}` 记入 `.git/publish_guard_pending.json`，
     返回 `push-pending`（含 commit SHA）、退出码 4；**不回退、不重复造 commit**；
   - 下次运行时，**在判断工作区 no-change 之前**先读取该状态并补推；补推成功 ->
     `push-pending-resolved`，清理状态；补推仍失败 -> 继续返回 `push-pending`；
   - 补推始终把「记录的那个提交」推到「记录的那个分支」（`<commit>:refs/heads/<branch>`），
     **不使用当前 HEAD**：即使之后本地又出现后继提交或切到别的分支，也不会把多余内容推上去；
   - 记录中的提交已不在当前分支历史（切分支 / detached HEAD / 本地 reset）时，**不静默清理**：
     先用 `git ls-remote` 确认它是否已在目标远端分支，已确认才清理；否则返回
     `push-pending-conflict`（退出码 5）要求人工处理。
     （按 Issue #3 复核 2026-10-09 夜间 与 2026-10-10 早间 P0 修正。）
   - **先落盘、再推送**：提交通过空提交复核后，先把 `{commit, branch, remote}` 用
     同目录临时文件 + flush/fsync + `os.replace` **原子写入**，然后才推送该确切提交；
     推送成功才清理记录。这样即使 `git push` 挂起时进程被直接终止（异常处理不运行），
     记录也已存在，下一轮仍能发现并只补推原 commit。
   - **损坏记录失败封闭**：记录文件存在但不可读 / 不是合法 JSON 对象时，返回
     `push-pending-conflict`（退出码 5），**不当作「没有 pending」**，也不清理原文件、
     不创建新提交。
     （按 Issue #3 复核 2026-10-10 下午 P0 修正。）

用途
----
    python3 scripts/publish_guard.py --preset daily --message "daily: ..." --push
    python3 scripts/publish_guard.py --message "story: ..." \
        --path index.html --path stories.html --path stories/

  - `--path`    本次发布的路径白名单，可重复；必填（或用 `--preset`）。
  - `--preset`  `daily` = daily.html + data/daily-published.json；
                `story` = index.html + stories.html + stories/ + daily.html
                + data/daily-published.json（全量同步会同时更新动态，见 sync_content.py）。
  - `--push`    提交后把当前分支推送到 `--remote`（默认 origin）。
  - `--dry-run` 只做比较与报告，不写索引、不提交、不推送。

行为与退出码
------------
  0  published               已提交（--dry-run 时为 would-publish）
  0  no-change               白名单暂存后与 HEAD 无差异：不提交、不推送
  0  push-pending-resolved   上一轮遗留的未推送提交已补推成功（本轮未新建提交）
  3  empty-commit-prevented  提交后复核发现 tree 与父相同，已 git reset --soft 回退
  4  push-pending            已创建提交但推送未确认（含 commit SHA），下次运行会先重试推送
  5  push-pending-conflict   待推送提交不在当前 HEAD 历史且无法确认已在目标远端，需人工处理
  2  用法或 git 操作错误
输出为单行 JSON，便于自动化按 `result` 判断；推送失败时另带 `push_error`，
与「未创建提交」的错误（result=error）在字段上区分开。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

PRESETS = {
    # 增量发布日常动态时，sync_content.py 只写这两个文件。
    "daily": ["daily.html", "data/daily-published.json"],
    # 全量同步：故事归档 + 故事页 + 首页入口，并同时同步日常动态。
    "story": ["index.html", "stories.html", "stories/",
              "daily.html", "data/daily-published.json"],
}


# 退出码：已创建提交、但推送未确认（见模块文档 5）。
PENDING_EXIT = 4
# 退出码：待推送记录失效（提交不在当前 HEAD 历史且无法确认已在远端），需人工处理。
PUSH_CONFLICT_EXIT = 5


class PublishError(Exception):
    """git 调用失败或用法错误。"""


def _run(args, cwd, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise PublishError("命令失败 %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def tree_of(rev, cwd):
    return _run(["git", "rev-parse", rev + "^{tree}"], cwd).stdout.strip()


def changed_paths(cwd, paths):
    """相对 HEAD、已暂存且落在白名单内的改动路径。"""
    diff = _run(["git", "diff", "--cached", "--name-only", "HEAD", "--"] + list(paths), cwd).stdout
    return sorted({ln.strip() for ln in diff.splitlines() if ln.strip()})


def tracked_changes(cwd, paths):
    """相对 HEAD 的已跟踪改动（含索引与工作树），供 dry-run 只读探测。"""
    diff = _run(["git", "diff", "--name-only", "HEAD", "--"] + list(paths), cwd).stdout
    return sorted({ln.strip() for ln in diff.splitlines() if ln.strip()})


def untracked_files(cwd, paths):
    """白名单内、不被 .gitignore 排除的未跟踪文件，供 dry-run 只读探测。"""
    out = _run(["git", "ls-files", "--others", "--exclude-standard", "--"] + list(paths),
               cwd).stdout
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def pending_changes(cwd, paths):
    """dry-run 只读探测：已跟踪改动 + 白名单内未跟踪文件，均相对 HEAD。"""
    return sorted(set(tracked_changes(cwd, paths)) | set(untracked_files(cwd, paths)))


def head_rev(cwd):
    return _run(["git", "rev-parse", "HEAD"], cwd).stdout.strip()


def current_branch(cwd):
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).stdout.strip()


def is_ancestor(cwd, rev, ref):
    """rev 是否为 ref 的祖先（含 rev == ref）。"""
    return _run(["git", "merge-base", "--is-ancestor", rev, ref],
                cwd, check=False).returncode == 0


def _push(cwd, remote, branch, source_rev="HEAD"):
    """把 source_rev（默认当前 HEAD）推送到远端同名分支；失败抛 PublishError。

    补推遗留提交时必须显式传入记录中的 commit，不能用当前 HEAD，
    否则会把后继提交或其它分支内容一起推上去（Issue #3 复核 2026-10-10 早间 P0）。
    """
    _run(["git", "push", remote, source_rev + ":refs/heads/" + branch], cwd)


def remote_branch_rev(cwd, remote, branch):
    """远端分支当前 tip 的提交 SHA；分支不存在或查询失败时返回 None。"""
    p = _run(["git", "ls-remote", "--heads", remote, "refs/heads/" + branch],
             cwd, check=False)
    if p.returncode != 0:
        return None
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    return lines[0].split()[0]


def state_path(cwd):
    """待推送状态文件路径（放在 .git 内，不进入版本历史）。"""
    git_dir = _run(["git", "rev-parse", "--absolute-git-dir"], cwd).stdout.strip()
    return os.path.join(git_dir, "publish_guard_pending.json")


def _pending_state_at(path):
    """读取指定路径的待推送状态：("absent"|"ok"|"corrupt", data)。

    文件不存在 -> ("absent", None)；合法 JSON 对象 -> ("ok", dict)；
    文件存在但不可读 / 不是合法 JSON / 不是 JSON 对象 -> ("corrupt", None)。
    绝不把损坏记录当成「没有 pending」（Issue #3 复核 2026-10-10 下午 P0）。
    """
    if not os.path.exists(path):
        return "absent", None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    return "ok", data


def pending_state(cwd):
    """当前仓库的待推送状态，区分 absent / ok / corrupt 三种情况。"""
    return _pending_state_at(state_path(cwd))


def load_pending(cwd):
    """兼容旧调用：合法记录返回 dict，其余（不存在或损坏）返回 None。"""
    status, data = pending_state(cwd)
    return data if status == "ok" else None


def save_pending(cwd, data):
    """原子写入待推送状态：同目录临时文件 -> flush + fsync -> os.replace。

    保证「写入过程中进程被终止」不会留下半截 JSON 被误读
    （Issue #3 复核 2026-10-10 下午 P0）。
    """
    path = state_path(cwd)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".publish_guard_pending.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def clear_pending(cwd):
    p = state_path(cwd)
    if os.path.exists(p):
        os.remove(p)


def commit_is_empty(cwd, rev="HEAD"):
    """HEAD 的 tree 是否与父提交相同（即空提交）。无父提交时返回 False。"""
    parent = _run(["git", "rev-parse", "--verify", rev + "^"], cwd, check=False)
    if parent.returncode != 0:
        return False
    return tree_of(rev, cwd) == tree_of(rev + "^", cwd)


def resolve_paths(preset, extra_paths):
    """合并 preset 与显式 --path，去重并保持顺序。"""
    merged = []
    if preset:
        merged.extend(PRESETS[preset])
    merged.extend(extra_paths or [])
    seen = set()
    out = []
    for p in merged:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def publish(message, paths, cwd, push=False, remote="origin", dry_run=False):
    """白名单暂存 -> 暂存区与 HEAD 比较 -> 提交 -> 可选推送。返回 (result_dict, exit_code)。"""
    paths = [p for p in (paths or []) if p]
    if not paths:
        raise PublishError("白名单必填：请给出 --path，或使用 --preset daily/story")

    out = {
        "result": "no-change",
        "paths": list(paths),
        "changed_paths": [],
        "commit": None,
        "commit_tree": None,
        "parent_tree": None,
        "pushed": False,
        "dry_run": bool(dry_run),
        "pending_push": False,
        "pending_commit": None,
        "note": None,
    }

    if dry_run:
        # 只读探测：不调用 git add，不改动索引，也不重试推送
        # （Issue #3 复核 2026-10-09 午后 P0）。
        changed = pending_changes(cwd, paths)
        pend_status, pend = pending_state(cwd)
        if pend_status == "corrupt":
            # 记录存在但损坏：dry-run 也只读地失败封闭，不清理、不新建提交。
            out["result"] = "push-pending-conflict"
            out["pending_push"] = True
            out["note"] = "待推送记录存在但不可读/格式损坏，需人工处理（dry-run 只读，不清理）"
            return out, PUSH_CONFLICT_EXIT
        if pend_status == "ok":
            out["pending_push"] = True
            out["pending_commit"] = pend.get("commit")
        if not changed:
            return out, 0
        out["changed_paths"] = changed
        out["result"] = "would-publish"
        return out, 0

    # 0. 先处理上一轮「已创建提交、推送未确认」的遗留状态
    #    （Issue #3 复核 2026-10-09 夜间 P0）：不能把它当作普通 no-change，
    #    要在比较工作区之前先重试推送。
    pending_resolved = False
    pend_status, pend = pending_state(cwd)
    if pend_status == "corrupt":
        # 记录存在但不可读 / 不是合法对象：失败封闭，不当作「没有 pending」，
        # 不创建新提交、不清理原文件（Issue #3 复核 2026-10-10 下午 P0）。
        out["result"] = "push-pending-conflict"
        out["pending_push"] = True
        out["note"] = "待推送记录存在但不可读/格式损坏，需人工处理；保留原文件、不新建提交"
        return out, PUSH_CONFLICT_EXIT
    if pend_status == "ok":
        commit = pend.get("commit")
        branch = pend.get("branch")
        pending_remote = pend.get("remote") or remote
        if commit:
            out["pending_commit"] = commit
            out["commit"] = commit
        if not commit or not branch:
            # 记录不完整：不静默丢弃，交人工处理。
            out["result"] = "push-pending-conflict"
            out["pending_push"] = True
            out["note"] = "待推送记录不完整（缺 commit 或 branch），需人工处理"
            return out, PUSH_CONFLICT_EXIT
        if is_ancestor(cwd, commit, "HEAD"):
            # 记录中的提交仍在当前 HEAD 历史：只补推它本身到记录的分支，
            # 不使用当前 HEAD，避免带上后继提交或其它分支内容。
            if not push:
                out["result"] = "push-pending"
                out["pending_push"] = True
                out["note"] = "存在已创建但未确认推送的提交；未提供 --push，本轮不重试推送"
                return out, PENDING_EXIT
            try:
                _push(cwd, pending_remote, branch, source_rev=commit)
            except PublishError as e:
                out["result"] = "push-pending"
                out["pending_push"] = True
                out["push_error"] = str(e)
                out["note"] = "上一轮已创建的提交仍未推送成功，保留待推送状态"
                return out, PENDING_EXIT
            clear_pending(cwd)
            pending_resolved = True
        else:
            # 记录中的提交不在当前 HEAD 历史（切分支 / detached HEAD / 本地 reset）。
            # 只有确认该提交已在目标远端分支时才清理；否则返回明确冲突，不静默继续。
            remote_tip = remote_branch_rev(cwd, pending_remote, branch)
            if remote_tip == commit:
                clear_pending(cwd)
                pending_resolved = True
                out["note"] = "待推送记录中的提交已在目标远端分支，清理记录"
            else:
                out["result"] = "push-pending-conflict"
                out["pending_push"] = True
                out["note"] = ("待推送提交不在当前 HEAD 历史，且目标远端分支未指向该提交；"
                               "保留记录，请人工处理")
                return out, PUSH_CONFLICT_EXIT

    # 1. 正式发布：只暂存白名单（含白名单内的新增文件与删除）。
    _run(["git", "add", "-A", "--"] + list(paths), cwd)
    # 2. 与当前 HEAD 比较暂存区；有差异才算本次发布。不依赖 origin/main。
    if _run(["git", "diff", "--cached", "--quiet", "HEAD", "--"] + list(paths),
            cwd, check=False).returncode == 0:
        if pending_resolved:
            out["result"] = "push-pending-resolved"
            out["pushed"] = True
        return out, 0

    out["changed_paths"] = changed_paths(cwd, paths)

    if not message:
        raise PublishError("有改动待提交，但未提供 --message")

    # 3. 提交只覆盖白名单：其他暂存内容不进入本次发布提交。
    _run(["git", "commit", "-m", message, "--"] + list(paths), cwd)
    out["commit"] = _run(["git", "rev-parse", "HEAD"], cwd).stdout.strip()
    out["commit_tree"] = tree_of("HEAD", cwd)
    parent = _run(["git", "rev-parse", "--verify", "HEAD^"], cwd, check=False)
    if parent.returncode == 0:
        out["parent_tree"] = tree_of("HEAD^", cwd)

    if commit_is_empty(cwd):
        # 复核：绝不把空提交留在历史里。
        _run(["git", "reset", "--soft", "HEAD^"], cwd)
        out["result"] = "empty-commit-prevented"
        out["commit"] = None
        out["commit_tree"] = None
        return out, 3

    out["result"] = "published"
    if push:
        branch = current_branch(cwd)
        # 先原子落盘「已提交、推送未确认」记录，再推送该确切提交：
        # 这样即使 push 挂起时进程被直接终止（异常处理不会运行），记录也已存在，
        # 下一轮仍能发现并只补推这个 commit（Issue #3 复核 2026-10-10 下午 P0）。
        save_pending(cwd, {"commit": out["commit"], "branch": branch, "remote": remote})
        try:
            _push(cwd, remote, branch, source_rev=out["commit"])
        except PublishError as e:
            # 推送未确认：保留记录与 commit，下一次运行先补推，
            # 绝不落进普通 no-change（Issue #3 复核 2026-10-09 夜间 P0）。
            out["result"] = "push-pending"
            out["pending_push"] = True
            out["push_error"] = str(e)
            return out, PENDING_EXIT
        clear_pending(cwd)
        out["pushed"] = True
    return out, 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布守卫：白名单无变化时不创建提交")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                    help="固定白名单：daily 或 story")
    ap.add_argument("--path", action="append", default=[],
                    help="白名单路径，可重复；与 --preset 合并")
    ap.add_argument("--message", default=None)
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)

    paths = resolve_paths(args.preset, args.path)
    if not paths:
        ap.error("白名单必填：请给出 --path，或使用 --preset daily/story")
    try:
        result, code = publish(args.message, paths, args.cwd,
                              push=args.push, remote=args.remote, dry_run=args.dry_run)
    except PublishError as e:
        print(json.dumps({"result": "error", "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
