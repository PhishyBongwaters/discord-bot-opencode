#!/usr/bin/env python3
"""Fence-aware Discord message chunking (stdlib-only).

split_smart(text, n=2000) splits long text into message-sized pieces that
never leave a fenced code block unbalanced: a chunk ending inside a fence
gets the closing fence appended, and the next chunk reopens the identical
fence (marker + info string). Long single lines hard-split at a space
boundary. Content is otherwise preserved byte-for-byte.
"""

import re

FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})(.*)$")


def _parse_fence(line):
    m = FENCE_RE.match(line)
    if not m:
        return None
    return m.group(2), m.group(3)  # marker, info string


def split_smart(text, n=2000):
    """Split text into pieces of at most n chars. Returns a list."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return []
    if len(text) <= n:
        return [text]

    chunks = []
    cur = []
    cur_len = 0
    fence = None  # [marker, info] while inside a fence

    def close_chunk():
        nonlocal cur, cur_len, fence
        if fence:
            cur.append(fence[0])  # closing fence
        chunks.append("\n".join(cur))
        cur = []
        cur_len = 0
        if fence:
            opener = fence[0] + fence[1]
            cur.append(opener)
            cur_len = len(opener) + 1

    for line in text.split("\n"):
        finfo = _parse_fence(line)

        # hard-split overlong lines (fence lines are never this long).
        # Inside a fence, split narrower to leave room for the
        # reopen + close markers that will wrap the piece.
        parts = [line]
        if len(line) > n and finfo is None:
            w = n if not fence else max(n - 2 * len(fence[0]) - 2, 1)
            parts = []
            s = line
            while len(s) > w:
                cut = s.rfind(" ", 0, w)
                cut = cut if cut > w // 2 else w
                parts.append(s[:cut])
                s = s[cut:]
            parts.append(s)

        for part in parts:
            cost = len(part) + 1
            # reserve room for the closing fence while inside one
            limit = n if not fence else n - len(fence[0]) - 1
            # need room for more than just a reopened fence line
            min_lines = 1 if fence else 0
            if cur_len + cost > limit and len(cur) > min_lines:
                close_chunk()
                limit = n if not fence else n - len(fence[0]) - 1
            cur.append(part)
            cur_len += cost

        if finfo is not None:
            if fence and finfo[0] == fence[0]:
                fence = None  # closing fence
            elif not fence:
                fence = [finfo[0], finfo[1]]  # opening fence
            # else: different marker inside a fence = literal code, ignore

    if cur:
        if fence:
            cur.append(fence[0])
        chunks.append("\n".join(cur))
    return [c for c in chunks if c]


def _self_test():
    # 1. short text untouched
    assert split_smart("hi") == ["hi"]
    assert split_smart("") == []
    # 2. exact boundary
    assert split_smart("a" * 2000) == ["a" * 2000]
    assert len(split_smart("a" * 2001, 2000)) == 2
    # 3. fence spanning chunks: balanced per chunk, content preserved
    code = "\n".join(f"line {i:04d} xxxxxxxxxxxxxxxxxxxx" for i in range(40))
    text = "intro\n```python\n" + code + "\n```\noutro"
    chunks = split_smart(text, 300)
    assert len(chunks) > 2, chunks
    assert all(len(c) <= 300 for c in chunks), [len(c) for c in chunks]
    for c in chunks:
        fences = [ln for ln in c.split("\n")
                  if _parse_fence(ln) is not None]
        assert len(fences) % 2 == 0, c
    def strip_fences(s):
        return "\n".join(ln for ln in s.split("\n")
                         if _parse_fence(ln) is None)
    assert strip_fences("\n".join(chunks)) == strip_fences(text)
    # reopen keeps info string
    assert any("```python" in c.split("\n")[0] for c in chunks[1:]), chunks
    # 4. unclosed fence passes through, never balanced by us
    t = "hello\n```\n" + "y" * 3000
    chunks = split_smart(t, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    # 5. long single line rejoins exactly
    long_line = "word " * 2000
    chunks = split_smart(long_line.strip(), 500)
    assert "".join(c.strip() for c in chunks).replace(" ", "") == \
        long_line.strip().replace(" ", "")
    assert all(len(c) <= 500 for c in chunks)
    # 6. no fences at all: newline-boundary splits
    t = "\n".join(f"para {i}" for i in range(200))
    chunks = split_smart(t, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).replace("\n", "") == t.replace("\n", "")
    print("chunking self-test OK")


if __name__ == "__main__":
    _self_test()
