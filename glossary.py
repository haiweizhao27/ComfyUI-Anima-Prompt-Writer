# -*- coding: utf-8 -*-
"""术语库检索 —— 把用户输入里命中的标准标签捞出来，注入提示词。

为什么这么做（而不是让模型记住词汇）：
  模型不可能背下 3 万条标签。但**检索不费算力也不费显存**。
  用户说「晚礼服」，我们查出 `evening gown` 直接塞进提示词，
  模型照抄即可 —— 这就是 RAG，比微调便宜得多，也不用训练。

匹配策略（三级，从精确到宽松）：
  1 精确词：输入的 2~12 字子串正好是库里的中文键
  2 长词优先：命中后从输入里划掉，避免「水手服」又被「水手」重复匹配
  3 角色单独走：角色库用英文名 + 中文名双索引
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = {}

# 视角/构图的口语说法 -> 标准标签。术语库里没有这些中文键，
# 但用户极爱这么写，不补就会乱造 ——
# 实测「视角为斜上方往下看」被译成 `upside-down perspective`（倒立），
# 这是**错译**不是漏译，比丢词更糟。
_ALIAS = {
    "斜上方": ("from above", "④环境"),
    "从上往下": ("from above", "④环境"),
    "由上往下": ("from above", "④环境"),
    "由下往上": ("from below", "④环境"),
    "低角度": ("from below", "④环境"),
    "俯视": ("from above", "④环境"),
    "俯瞰": ("from above", "④环境"),
    "鸟瞰": ("bird's-eye view", "④环境"),
    "仰视": ("from below", "④环境"),
    "背影": ("from behind", "④环境"),
    "侧脸": ("profile", "①角色"),
}


def load(path=None):
    global CACHE
    p = path or os.path.join(HERE, "glossary.json")
    if p not in CACHE:
        d = json.load(open(p, encoding="utf-8"))
        # ⚠️ `themes_v21.json` 里标成**⑤画风**的条目是错的 —— 它的 block 是猜的。
        #    实测错标：`盘腿坐 → ⑤画风`（"indian style" 在英语里是**盘腿坐**，
        #    不是画风；结果出图时画风块里被塞进"印度风格构图"）、`透明 → ⑤画风`。
        #    **只清空这一种**：全清会连 `冰淇淋 → ice cream` 一起跳过，
        #    而出图已经证明丢掉冰淇淋是硬伤。
        #    block="" 表示"来源不可信"：render 标「通用」、block_of 返回 None、
        #    回填跳过（宁可不补，也不能补到错的块里）。
        for t in d["tags"]:
            if t.get("src") == "themes" and "画风" in str(t.get("block", "")):
                t["block"] = ""
        # 角色库是**可选的**：开源版 glossary.json 里不含（那部分的版权来源
        # 更复杂），想用角色功能就在同目录放一个 chars.json（已 gitignore）。
        # 这样仓库存的是"安全子集"，本机功能一点不减。
        cp = os.path.join(os.path.dirname(p), "chars.json")
        if os.path.exists(cp):
            try:
                c = json.load(open(cp, encoding="utf-8"))
                d["chars"] = c.get("chars", [])
                d["char_index"] = c.get("char_index", {})
            except Exception as e:
                print("[glossary] chars.json 读取失败，角色功能不可用: %s" % e)
        d.setdefault("chars", [])
        d.setdefault("char_index", {})
        keys = sorted(d["tag_index"], key=len, reverse=True)
        d["_keys"] = keys
        d["_maxlen"] = max((len(k) for k in keys), default=4)
        CACHE[p] = d
    return CACHE[p]


def retrieve(text, top=10, block=None, chars_top=2):
    """返回 (命中标签, 命中角色)。

    命中标签：[{zh, en, block, src}, ...] 按在原文出现的位置排序，去重
    命中角色：[{en, zh, copyright, hair, eye}, ...]
    """
    g = load()
    idx, tags = g["tag_index"], g["tags"]
    mark = [False] * len(text)
    hits, seen, spans = [], set(), []
    keyset = set(g["_keys"])
    maxlen = g["_maxlen"]

    # 从每个位置起，先试长词（最长 12 字）
    for i in range(len(text)):
        if mark[i]:
            continue
        for L in range(min(maxlen, len(text) - i), 1, -1):
            w = text[i:i + L]
            if w in keyset and not any(mark[i:i + L]):
                for j in idx[w]:
                    t = tags[j]
                    if block and t["block"] != block:
                        continue
                    # ⚠️ 去重键必须**带 block**。原来只用 (zh, en)：
                    #    `pink` 在 background 里是 ④环境、在 clothing 里是 ②衣着，
                    #    键相同 -> 后一条被丢掉，只剩 ④环境那条，
                    #    于是模型把 `pink and purple hues` 写进了环境块（实测）。
                    k = (t["zh"], t["en"], t["block"])
                    if k in seen:
                        continue
                    seen.add(k)
                    hits.append({"zh": t["zh"], "en": t["en"],
                                 "block": t["block"], "src": t["src"],
                                 "pos": i})
                for m in range(i, i + L):
                    mark[m] = True
                spans.append((i, i + L))
                break

    hits.sort(key=lambda h: h["pos"])
    hits = [h for h in hits if not h["en"].endswith("_")][:top]

    # 口语化的视角说法，库里没有对应中文键 —— 不补的话模型会自己乱造。
    # 实测「视角为斜上方往下看」被译成了 `upside-down perspective`（倒立），
    # 是**错译**不是漏译，比丢词更糟。
    for zh, (en, blk) in _ALIAS.items():
        p = text.find(zh)
        if p >= 0 and (zh, en, blk) not in seen:
            seen.add((zh, en, blk))
            hits.append({"zh": zh, "en": en, "block": blk,
                         "src": "alias", "pos": p})
    hits.sort(key=lambda h: h["pos"])
    hits = hits[:top + len(_ALIAS)]

    # 角色（**可选**：开源版没有角色库。用 .get 兜住，缺了就当没有角色命中，
    # 绝不能 KeyError 把整个检索搞挂）
    ci, chars = g.get("char_index") or {}, g.get("chars") or []
    chits, cseen = [], set()
    for k, ids in ci.items():
        # 单字中文名会撞车：输入「逆光」里的「光」曾匹配到角色「光 (mythra)」。
        # 所以中文名至少 2 字，纯英文名至少 4 字。
        if re.search(r"[\u4e00-\u9fff]", k):
            if len(k) < 2:
                continue
        elif len(k) < 4:
            continue
        if "(append)" in k or k.endswith("(series)"):
            continue                      # 衍生/系列条目，不是具体角色
        # ⚠️ 角色名与**标签名**撞车时必须让给标签（踩过）：
        #    颜色「红色」正好是角色 `red (among us)`（Among Us）的中文名，
        #    于是「红色透明塑料夹克」把 Among Us 塞进了①角色。
        #    颜色、常见物件名这类"同时也是标签"的词，一律不当角色。
        if k in g["tag_index"]:
            continue
        # ⚠️ 角色名必须**完整落在标签命中之外**，不能从两个标签的接缝里读出来。
        #    实测：原文「樱花花瓣」里相连的「花花」被当成了角色
        #    「花花 (powerpuff girls)」—— 跨越 樱花 / 花瓣 两个标签的边界。
        #    规则：只要某个出现位置与任一个标签区间**部分重叠**，就说明是接缝产物。
        at, ok = text.find(k), False
        while at != -1:
            end = at + len(k)
            partial = any(s < end and at < e and not (s <= at and end <= e)
                          for s, e in spans)
            if not partial:
                ok = True
                break
            at = text.find(k, at + 1)
        if ok:
            for j in ids:
                c = chars[j]
                if c["en"] in cseen or "(append)" in c["en"]:
                    continue
                cseen.add(c["en"])
                chits.append(c)
    chits.sort(key=lambda c: -c.get("posts", 0))
    return hits, chits[:chars_top]


def _norm(s):
    """统一成**空格**分隔。

    anima 的编码器是 Qwen3（语言模型），吃自然语言 ——
    实测下划线版把画面里的"狗"整个丢了，空格版就出来了。
    术语库里两种写法混着有（`blue sky` 与 `blue_sky`），这里统一。
    """
    return re.sub(r"\s+", " ", str(s).replace("_", " ")).strip()


def block_of(en):
    """英文标签 -> 术语库认定的**纯块名**（`衣着` / `角色` …）。查不到返回 None。

    用途：同一条标签同时出现在两个块里时，用库里的归类来裁决该留哪个块。
    实测 `waist apron` 库里就是②衣着，于是围裙不会留在①角色里。
    """
    g = load()
    if "_blk" not in g:
        m = {}
        for t in g["tags"]:
            k = _norm(t["en"]).lower()
            b = re.sub(r"^[①②③④⑤]\s*", "", str(t["block"])).strip()
            if k and b:
                m.setdefault(k, b)      # 先出现的归类优先
        g["_blk"] = m
    return g["_blk"].get(_norm(en).lower())


def _key(s):
    """同义写法去重键：忽略大小写/下划线，并抹掉词尾复数。

    「樱花」在库里一次能查到 4 个写法（cherry blossoms / sakura /
    cherry blossom / cherry_blossoms），全给模型它就会全抄进输出 ——
    实测输出里同时出现 `cherry blossom tree` 和 `sakura tree`。
    """
    return re.sub(r"s\b", "", _norm(s).lower())


def render(hits, chits, max_per_tag=1):
    """拼成给模型看的「参考标签」段。返回空串表示无命中。"""
    if not hits and not chits:
        return ""
    lines = ["【术语库命中 —— 优先采用下面这些标准写法，不要再自己造词】"]
    # ⚠️ 上限别设太小（踩过）：原来 [:12]，而命中是按**在原文出现的先后**排的，
    #    于是排在后面的词被整段截掉。用户那句「…流进了乳沟…」里，
    #    `乳沟 → cleavage` 正好在第 13 组之后 —— 模型压根没看到，必然丢掉。
    #    现在放到 24，配合节点侧 top 一起放宽。
    byzh = {}
    for h in hits:
        v = byzh.setdefault(h["zh"], {"en": [], "key": set(), "block": []})
        e = _norm(h["en"])
        kk = _key(e)
        # 一个中文最多给 2 个英文写法，且按去重键排掉复数变体
        if e and kk not in v["key"] and len(v["en"]) < 2:
            v["en"].append(e)
            v["key"].add(kk)
        # ⚠️ 空 block 不收集（否则 v["block"] 会是 [""]，非空列表 -> 判断失效，
        #    界面上显示成空的 `（）`。踩过。）
        if h["block"] and h["block"] not in v["block"]:
            v["block"].append(h["block"])
    for zh, v in list(byzh.items())[:24]:
        if v["en"]:
            # 跨块出现的词（颜色最典型）标成「通用」，别把模型往某一个块上带。
            # ⚠️ 只按"跨块"判断不够：库里 `粉色→pink` 只有 ④环境 一条记录
            #    （它来自 background_data.js），跨不了块，可颜色显然不属于环境 ——
            #    实测模型因此把 `pink and purple hues` 写进了环境块。
            #    中文颜色词几乎都以「色」结尾，用它兜住。
            #    另外 block 为空串表示"来源不可信"（themes_v21.json），也标通用。
            if not v["block"] or len(v["block"]) > 1 or zh.endswith("色"):
                blk = "通用"
            else:
                blk = v["block"][0]
            lines.append("  %s（%s）→ %s" % (zh, blk, ", ".join(v["en"])))
    for c in chits:
        # ⚠️ 属性要**带标签名**并去重。原来直接拼 copyright/hair/eye，
        #    刻晴出来是 `genshin impact purple purple`（发色瞳色都是紫，
        #    重复且看不出哪个是头发）-> 改成 `genshin impact, purple hair, purple eyes`。
        attrs = []
        if c.get("copyright"):
            attrs.append(_norm(c["copyright"]))
        if c.get("hair"):
            attrs.append(_norm(c["hair"]) + " hair")
        if c.get("eye"):
            attrs.append(_norm(c["eye"]) + " eyes")
        seen_a, uniq = set(), []
        for a in attrs:
            if a.lower() not in seen_a:
                seen_a.add(a.lower())
                uniq.append(a)
        lines.append("  角色 %s%s → %s%s"
                     % (_norm(c["en"]), "（%s）" % c["zh"] if c["zh"] else "",
                        _norm(c["en"]), ", " + ", ".join(uniq) if uniq else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for q in ["一名穿着晚礼服的少女，侧开叉，站在海滩的吊床上",
              "穿着水手服的少女坐在教室里，旁边是和服少女",
              "初音未来在舞台上唱歌",
              "一个红发双马尾的少女，过膝袜，水手服，蓝天，逆光"]:
        h, c = retrieve(q)
        print("  [%s]" % q)
        print("    " + (render(h, c) or "（无命中）").replace("\n", "\n    "))
        print()
