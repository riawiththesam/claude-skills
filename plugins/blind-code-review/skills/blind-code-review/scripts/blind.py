#!/usr/bin/env python3
"""blind.py - preprocessor for blind code review.

Produces a view of a change that contains only machine-verified material:
implementation code and config, with comments removed, and with tests,
prose (Markdown, docs, plans) and lockfiles excluded. Never reads PR title,
body, commit messages or branch names.

  blind.py prep --pr N [--remote origin] [--out DIR] [--context 6]
  blind.py prep --base REF [--head REF] [--out DIR] [--context 6]
  blind.py show --dir DIR PATH          # stripped file at head
  blind.py ls   --dir DIR [SUBDIR]      # list files at head (tests/prose hidden)
  blind.py grep --dir DIR STRING        # fixed-string search in stripped code at head
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- classify

TS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}
KT_EXT = {".kt", ".kts"}
C_EXT = {".java", ".json", ".jsonc", ".json5"}
HASH_EXT = {".yml", ".yaml", ".toml", ".properties", ".sh", ".bash", ".zsh", ".env", ".pro"}
HASH_NAMES = {"Dockerfile", ".gitignore", ".npmrc", ".nvmrc", ".editorconfig", ".dockerignore", "Makefile"}
XML_EXT = {".xml"}
PROSE_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc"}
PROSE_NAME = re.compile(r"^(LICEN[CS]E|CHANGELOG|CHANGES|NOTICE|AUTHORS|HISTORY)(\..*)?$", re.I)
LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
              "gradle.lockfile", "npm-shrinkwrap.json"}
TEST_DIR = re.compile(r"^(__tests__|__mocks__|__snapshots__|__fixtures__|tests?|specs?|e2e|cypress|"
                      r"playwright|fixtures|testdata|test-?utils|testFixtures|testing)$")
TEST_SRCSET = re.compile(r"^[a-z][A-Za-z]*Tests?$")  # androidTest, commonTest, jvmTest, androidUnitTest...
TEST_FILE = re.compile(r"(\.(test|spec|e2e|cy)\.[cm]?[jt]sx?$)|((Test|Tests|Spec|IT|TestCase)\.(kt|kts|java)$)"
                       r"|(\.snap$)|(^(jest|vitest|playwright|cypress|karma)\.config\.)|(^setupTests\.)")


def classify(path):
    """Return (category, lang). category: code | test | prose | lock."""
    parts = path.split("/")
    name = parts[-1]
    ext = os.path.splitext(name)[1].lower()
    if name in LOCK_NAMES:
        return "lock", None
    if ext in PROSE_EXT or PROSE_NAME.match(name) or "docs" in parts[:-1]:
        return "prose", None
    if any(TEST_DIR.match(p) or TEST_SRCSET.match(p) for p in parts[:-1]) or TEST_FILE.search(name):
        return "test", None
    if ext in TS_EXT:
        return "code", "ts"
    if ext in KT_EXT:
        return "code", "kt"
    if ext in C_EXT:
        return "code", "c"
    if ext in HASH_EXT or name in HASH_NAMES:
        return "code", "hash"
    if ext in XML_EXT:
        return "code", "xml"
    return "code", None  # unknown: kept, comments NOT stripped


# ------------------------------------------------------------------- strip

TS_REGEX_KW = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
               "case", "do", "else", "yield", "await"}
TS_REGEX_PUNCT = set("(,=:[!&|?{};+-*%<>~^")


class Stripper:
    """Lexer that blanks comments while keeping every newline (line numbers stay identical)."""

    def __init__(self, src, lang):
        self.s, self.n, self.lang = src, len(src), lang
        self.out = list(src)
        self.last = ""

    def run(self):
        self.code(0, False)
        return "".join(self.out)

    def blank(self, a, b):
        for k in range(a, b):
            if self.out[k] != "\n":
                self.out[k] = " "

    def regex_allowed(self):
        return self.last == "" or self.last in TS_REGEX_PUNCT or self.last in TS_REGEX_KW

    def code(self, i, in_tpl):
        s, n, depth = self.s, self.n, 0
        while i < n:
            c = s[i]
            if c == "/" and s.startswith("//", i):
                j = s.find("\n", i)
                j = n if j == -1 else j
                self.blank(i, j)
                i = j
                continue
            if c == "/" and s.startswith("/*", i):
                j = self.block_comment(i)
                self.blank(i, j)
                i = j
                continue
            if c in "\"'" or (c == "`" and self.lang == "ts"):
                i = self.string(i)
                self.last = "str"
                continue
            if c == "`" and self.lang == "kt":  # backtick identifier
                j = s.find("`", i + 1)
                i = n if j == -1 else j + 1
                self.last = "id"
                continue
            if c == "/" and self.lang == "ts" and self.regex_allowed():
                j = self.regex(i)
                if j is not None:
                    i = j
                    self.last = "str"
                    continue
            if c == "{":
                depth += 1
            elif c == "}":
                if in_tpl and depth == 0:
                    return i + 1
                depth -= 1
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (s[j].isalnum() or s[j] in "_$"):
                    j += 1
                self.last = s[i:j]
                i = j
                continue
            if not c.isspace():
                self.last = c
            i += 1
        return i

    def block_comment(self, i):
        s, n, j, depth = self.s, self.n, i + 2, 1
        while j < n:
            if s.startswith("*/", j):
                depth -= 1
                j += 2
                if depth == 0 or self.lang != "kt":
                    return j
                continue
            if self.lang == "kt" and s.startswith("/*", j):
                depth += 1
                j += 2
                continue
            j += 1
        return n

    def string(self, i):
        s, n, q = self.s, self.n, self.s[i]
        if self.lang in ("kt", "c") and s.startswith('"""', i):  # Kotlin raw string / Java text block
            j = i + 3
            while j < n:
                if self.lang == "c" and s[j] == "\\":
                    j += 2
                    continue
                if s.startswith('"""', j):
                    j += 3
                    while j < n and s[j] == '"':
                        j += 1
                    return j
                if self.lang == "kt" and s.startswith("${", j):
                    j = self.code(j + 2, True)
                    continue
                j += 1
            return n
        template = q == "`" or (self.lang == "kt" and q == '"')
        j = i + 1
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == q:
                return j + 1
            if template and s.startswith("${", j):
                j = self.code(j + 2, True)
                continue
            if c == "\n" and q != "`":
                return j  # unterminated literal: stop at end of line
            j += 1
        return n

    def regex(self, i):
        s, n, j, in_class = self.s, self.n, i + 1, False
        while j < n:
            c = s[j]
            if c == "\n":
                return None
            if c == "\\":
                j += 2
                continue
            if in_class:
                if c == "]":
                    in_class = False
            elif c == "[":
                in_class = True
            elif c == "/":
                j += 1
                while j < n and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                return j
            j += 1
        return None


def strip_hash(src):
    # full-line comments only; inline '#' is too ambiguous to remove safely
    return "\n".join("" if re.match(r"^\s*#", ln) else ln for ln in src.split("\n"))


def strip_xml(src):
    return re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.S)


def strip(src, lang):
    if lang in ("ts", "kt", "c"):
        out = Stripper(src, lang).run()
    elif lang == "hash":
        out = strip_hash(src)
    elif lang == "xml":
        out = strip_xml(src)
    else:
        out = src
    return "\n".join(ln.rstrip() for ln in out.split("\n"))


# --------------------------------------------------------------------- git

def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def git(*args, check=True):
    return run(["git", *args], check=check).decode()


def blob(rev, path):
    """Return text, or None if missing; b'' marker handled by caller for binary."""
    r = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False  # binary


# ------------------------------------------------------------------ render

def render(old, new, ctx):
    a = old.split("\n") if old else []
    b = new.split("\n") if new else []
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [(" ", i1 + k + 1, j1 + k + 1, a[i1 + k]) for k in range(i2 - i1)]
        else:
            rows += [("-", k + 1, None, a[k]) for k in range(i1, i2) if a[k].strip()]
            rows += [("+", None, k + 1, b[k]) for k in range(j1, j2) if b[k].strip()]
    changed = [i for i, r in enumerate(rows) if r[0] != " "]
    if not changed:
        return None
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - ctx), min(len(rows), i + ctx + 1)))
    out, prev = [], -2
    for i in sorted(keep):
        if i != prev + 1:
            out.append("   ...")
        tag, o, nn, t = rows[i]
        out.append(f"{o or '':>6} {nn or '':>6} {tag}| {t}")
        prev = i
    return "\n".join(out)


# -------------------------------------------------------------------- prep

def cmd_prep(args):
    if args.pr:
        info = json.loads(run(["gh", "pr", "view", str(args.pr), "--json",
                               "number,headRefOid,baseRefOid,baseRefName"]))
        run(["git", "fetch", "--no-tags", "-q", args.remote,
             f"pull/{args.pr}/head", info["baseRefName"]])
        head, base_tip = info["headRefOid"], info["baseRefOid"]
        label = f"pr{args.pr}"
    else:
        head = git("rev-parse", args.head).strip()
        base_tip = git("rev-parse", args.base).strip()
        label = head[:10]
    base = git("merge-base", base_tip, head).strip()
    top = git("rev-parse", "--show-toplevel").strip()
    out = args.out or os.path.join(tempfile.gettempdir(), "blind-review",
                                   f"{os.path.basename(top)}-{label}")
    os.makedirs(os.path.join(out, "head"), exist_ok=True)

    files, counts, sections = [], {"test": 0, "prose": 0, "lock": 0, "binary": 0}, []
    toks = [t for t in git("diff", "--name-status", "-M", "-z", base, head).split("\0") if t != ""]
    i = 0
    while i < len(toks):
        st = toks[i]
        if st[0] in "RC":
            old_p, new_p, i = toks[i + 1], toks[i + 2], i + 3
        else:
            old_p = new_p = toks[i + 1]
            i += 2
        cat, lang = classify(new_p)
        entry = {"path": new_p, "old_path": old_p, "status": st[0], "category": cat, "lang": lang}
        files.append(entry)
        if cat != "code":
            counts[cat] += 1
            continue
        old = blob(base, old_p) if st[0] != "A" else ""
        new = blob(head, new_p) if st[0] != "D" else ""
        if old is False or new is False:
            counts["binary"] += 1
            entry["category"] = "binary"
            continue
        old, new = strip(old or "", lang), strip(new or "", lang)
        if st[0] != "D":
            dst = os.path.join(out, "head", new_p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(new)
        body = render(old, new, args.context)
        if body is None:
            entry["comment_only_change"] = True
            continue
        status = {"A": "added", "D": "deleted", "M": "modified", "R": "renamed", "C": "copied",
                  "T": "type changed"}.get(st[0], st[0])
        hdr = f"=== {new_p}  ({status}" + (f" from {old_p}" if old_p != new_p else "") + ")"
        if lang is None:
            hdr += "  [WARNING: comments NOT stripped for this file type - treat any comment as unverified]"
        sections.append(hdr + "\n" + body)

    summary = [f"# blind diff  base={base[:10]} head={head[:10]}",
               f"# columns: old-line new-line | code   (comments removed, line numbers match real files)",
               f"# excluded: {counts['test']} test files, {counts['prose']} docs/prose files, "
               f"{counts['lock']} lockfiles, {counts['binary']} binary files",
               f"# comment-only changes (no code change): "
               f"{sum(1 for f in files if f.get('comment_only_change'))} files", ""]
    with open(os.path.join(out, "diff.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n" + "\n\n".join(sections) + "\n")
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"head": head, "base": base, "pr": args.pr, "files": files}, f, indent=1)
    print(out)
    print(f"code files with changes: {len(sections)}; excluded: {counts}")


# ------------------------------------------------------- show / ls / grep

def load(d):
    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def visible(path):
    return classify(path)[0] == "code"


def cmd_show(args):
    m = load(args.dir)
    if not visible(args.path):
        sys.exit(f"{args.path}: hidden in blind phase (test/prose/lock)")
    src = blob(m["head"], args.path)
    if src is None:
        sys.exit(f"{args.path}: not found at head")
    if src is False:
        sys.exit(f"{args.path}: binary")
    lang = classify(args.path)[1]
    if lang is None:
        print("# WARNING: comments NOT stripped for this file type - treat any comment as unverified")
    for no, ln in enumerate(strip(src, lang).split("\n"), 1):
        print(f"{no:>6}| {ln}")


def cmd_ls(args):
    m = load(args.dir)
    cmd = ["ls-tree", "-r", "--name-only", m["head"]]
    if args.subdir:
        cmd += ["--", args.subdir]
    for p in git(*cmd).splitlines():
        if visible(p):
            print(p)


def cmd_grep(args):
    m = load(args.dir)
    r = subprocess.run(["git", "grep", "-l", "-F", "-e", args.string, m["head"]], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        path = line.split(":", 1)[1]
        if not visible(path):
            continue
        src = blob(m["head"], path)
        if not src:
            continue
        for no, ln in enumerate(strip(src, classify(path)[1]).split("\n"), 1):
            if args.string in ln:
                print(f"{path}:{no}: {ln.strip()}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--pr", type=int)
    p.add_argument("--remote", default="origin")
    p.add_argument("--base")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--out")
    p.add_argument("--context", type=int, default=6)
    for name in ("show", "ls", "grep"):
        q = sub.add_parser(name)
        q.add_argument("--dir", required=True)
        q.add_argument({"show": "path", "ls": "subdir", "grep": "string"}[name],
                       nargs="?" if name == "ls" else None)
    a = ap.parse_args()
    if a.cmd == "prep":
        if not a.pr and not a.base:
            ap.error("prep needs --pr N or --base REF")
        cmd_prep(a)
    else:
        {"show": cmd_show, "ls": cmd_ls, "grep": cmd_grep}[a.cmd](a)


if __name__ == "__main__":
    main()
