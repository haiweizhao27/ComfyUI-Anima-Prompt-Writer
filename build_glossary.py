# -*- coding: utf-8 -*-
"""重建术语库 glossary.json（标签库）。

数据来源
--------
**必需**  Comfyui-Anima-Tools 的 `js/{clothing,pose,background}_data.js`
          https://github.com/luguoyin/Comfyui-Anima-Tools  （MIT License）
          它带中文对照、按套装打包，是「中文口语 → 标准标签」的最佳映射。

**可选**  `themes_v21.json` —— 放在本脚本同目录，补充带块归属的标签。
**可选**  `character_data.js` + `sm_all.json` —— 用 `--chars` 生成角色库
          `chars.json`（角色库不随仓库分发，见 README「角色库（可选）」）。

用法
----
    python build_glossary.py --anima-tools /path/to/Comfyui-Anima-Tools-main
    python build_glossary.py --chars              # 额外生成 chars.json
    python build_glossary.py --out /tmp/test.json # 输出到别处，不改动现用的库

也可以不传 `--anima-tools`：会依次尝试环境变量 `ANIMA_TOOLS_DIR`、
以及 ComfyUI 的 `custom_nodes/Comfyui-Anima-Tools-main`。
"""
import argparse
import collections
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCK_OF = {"clothing": "②衣着", "pose": "③动作", "background": "④环境"}


def find_anima_tools(explicit):
    """按 显式参数 -> 环境变量 -> ComfyUI 同级 custom_nodes 的顺序找。"""
    cands = [explicit, os.environ.get("ANIMA_TOOLS_DIR", "")]
    # 本插件在 ComfyUI/custom_nodes/<本插件>/，所以源项目应该在它的同级
    parent = os.path.dirname(HERE)
    cands += [os.path.join(parent, n) for n in
              ("Comfyui-Anima-Tools-main", "Comfyui-Anima-Tools",
               "ComfyUI-Anima-Tools", "comfyui-anima-tools")]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "js")):
            return c
    return ""


def load_js(path):
    """Anima-Tools 的 *_data.js 是 `xxx = [ ... ]` 形式的 JS，取出数组部分。"""
    if not os.path.exists(path):
        return []
    s = open(path, encoding="utf-8").read()
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < i:
        return []
    try:
        return json.loads(s[i:j + 1])
    except Exception as e:
        print("    [跳过] %s: %s" % (os.path.basename(path), e))
        return []


def split_tags(s):
    return [t.strip() for t in re.split(r"[,，]", s or "") if t.strip()]


def build_tags(js_dir, here):
    """标签库：tags[] 逐标签对照，bundles[] 整套装。"""
    tags, bundles = [], []
    counts = collections.Counter()

    for fn, key in (("clothing_data.js", "clothing"),
                    ("pose_data.js", "pose"),
                    ("background_data.js", "background")):
        for it in load_js(os.path.join(js_dir, fn)):
            en_list = split_tags(it.get("tags"))
            zh_list = split_tags(it.get("tags_zh"))
            if not en_list:
                continue
            blk = BLOCK_OF[key]
            cat = (it.get("categories") or [""])[0]
            # tags 与 tags_zh 是**逐项对齐**的，所以能拿到精确的逐标签对照。
            # 只做套装级会出事：抽检「水手服」曾把 `backpack, randoseru,
            # bottomless` 整包带出来。
            if len(zh_list) == len(en_list):
                for z, e in zip(zh_list, en_list):
                    z = re.sub(r"\(.*?\)", "", z).strip()
                    z = re.sub(r"[/／].*$", "", z).strip()
                    if 2 <= len(z) <= 12:
                        tags.append({"zh": z, "en": e, "block": blk,
                                     "src": key, "cat": cat})
                        counts["逐标签对照"] += 1
            zh_name = (it.get("name_zh") or it.get("name") or "").strip()
            if zh_name:
                bundles.append({"zh": zh_name, "tags": en_list,
                                "cat": cat, "block": blk, "src": key})
            counts[key] += 1

    # 可选的本地词表：带块归属，是 Anima-Tools 没有的部分
    tp = os.path.join(here, "themes_v21.json")
    if os.path.exists(tp):
        for v in json.load(open(tp, encoding="utf-8"))["themes"].values():
            for e in v["tags"]:
                tags.append({"zh": v["zh"], "en": e, "block": v["block"],
                             "src": "themes", "cat": ""})
                counts["themes"] += 1
    return tags, bundles, counts


def build_chars(js_dir, here):
    """角色库（+ 可选的中文名补充）。返回 chars 列表。"""
    chars = []
    for it in load_js(os.path.join(js_dir, "character_data.js")):
        nm = (it.get("name") or "").strip()
        if nm:
            chars.append({"en": nm, "zh": "", "copyright": it.get("copyright", ""),
                          "hair": it.get("hair", ""), "eye": it.get("eye", ""),
                          "posts": it.get("post_count", 0)})
    # sm_all.json 里有 27.8 万条中英对，用来给角色补中文名
    sp = os.path.join(here, "sm_all.json")
    en2zh = {}
    if os.path.exists(sp):
        for en, zh, cat, _c in json.load(open(sp, encoding="utf-8")):
            if cat == 4 and en not in en2zh:
                z = re.sub(r"\s*\(.*?\)\s*", "", str(zh)).strip()
                if z:
                    en2zh[en] = z
    n = 0
    for c in chars:
        z = en2zh.get(c["en"].replace(" ", "_")) or en2zh.get(c["en"])
        if z:
            c["zh"] = z
            n += 1
    return chars, n


def index_of(tags, chars):
    """中文名 -> 条目下标的倒排索引（检索用）。"""
    ti = collections.defaultdict(list)
    for i, t in enumerate(tags):
        if re.search(r"[\u4e00-\u9fff]", t["zh"]):
            ti[t["zh"]].append(i)
    ci = collections.defaultdict(list)
    for i, c in enumerate(chars):
        for k in (c["en"], c["zh"]):
            if k:
                ci[k].append(i)
    return dict(ti), dict(ci)


def main():
    ap = argparse.ArgumentParser(description="重建术语库 glossary.json")
    ap.add_argument("--anima-tools", default="",
                    help="Comfyui-Anima-Tools 的目录（含 js/ 子目录）")
    ap.add_argument("--out", default=os.path.join(HERE, "glossary.json"),
                    help="标签库输出路径（默认覆盖本目录的 glossary.json）")
    ap.add_argument("--chars", action="store_true",
                    help="额外生成角色库 chars.json（需要 character_data.js）")
    ap.add_argument("--chars-out", default=os.path.join(HERE, "chars.json"),
                    help="角色库输出路径")
    a = ap.parse_args()

    src = find_anima_tools(a.anima_tools)
    if not src:
        print("找不到 Comfyui-Anima-Tools。请用 --anima-tools 指定目录，"
              "或设置环境变量 ANIMA_TOOLS_DIR。")
        return 2
    js_dir = os.path.join(src, "js")
    print("数据来源: %s" % src)

    tags, bundles, counts = build_tags(js_dir, HERE)
    if not tags:
        print("没解析到任何标签 —— 确认 %s 里有 clothing/pose/background_data.js"
              % js_dir)
        return 1

    chars, n_zh = [], 0
    if a.chars:
        chars, n_zh = build_chars(js_dir, HERE)
        counts["角色"] = len(chars)
        counts["角色补到中文名"] = n_zh

    ti, ci = index_of(tags, chars)
    out = {"meta": {"counts": dict(counts), "tags": len(tags),
                    "bundles": len(bundles), "chars": len(chars)},
           "tags": tags, "bundles": bundles, "tag_index": ti}

    # 角色库**单独成文件**：它不随仓库分发（版权来源更复杂），
    # 而 glossary.py 会在同目录发现 chars.json 时自动合并。
    json.dump(out, open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print("  === 标签库 ===")
    for k, v in counts.items():
        print("    %-16s %6d" % (k, v))
    print("    %-16s %6d" % ("逐标签条目", len(tags)))
    print("    %-16s %6d" % ("套装", len(bundles)))
    print("    %-16s %6d" % ("中文检索键", len(ti)))
    print("  -> %s  (%.2f MB)" % (a.out, os.path.getsize(a.out) / 1048576.0))

    if a.chars:
        json.dump({"chars": chars, "char_index": ci},
                  open(a.chars_out, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        print("  === 角色库 ===")
        print("    %-16s %6d" % ("角色", len(chars)))
        print("    %-16s %6d" % ("索引键", len(ci)))
        print("  -> %s  (%.2f MB)" % (a.chars_out,
                                      os.path.getsize(a.chars_out) / 1048576.0))

    print("\n  === 抽检 ===")
    for w in ["晚礼服", "水手服", "和服", "冰淇淋", "刻晴"]:
        if w in ti:
            print("    %-8s -> %s" % (w, ", ".join(tags[i]["en"] for i in ti[w][:5])))
        elif w in ci:
            c = chars[ci[w][0]]
            print("    %-8s -> 角色: %s | %s" % (w, c["en"], c["copyright"]))
        else:
            print("    %-8s -> 没命中" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
