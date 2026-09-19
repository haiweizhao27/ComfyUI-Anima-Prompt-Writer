# -*- coding: utf-8 -*-
"""接地检查 —— 判断输出标签在源文本里有没有依据。

为什么需要：
  实测编造有**两个来源**：
    1 拓展阶段（把"室内"补成"教室课桌黑板"）—— 靠人工编辑拓展框能治
    2 **翻译阶段**（把"室内"翻成 `clean walls, white ceiling`）—— 编辑治不了
  这个模块治第二类：拿术语库**反查**，看每个标签对应的中文在源文本里有没有。

判定三级（关键是第三级**不能删**）：
  接地   反查到的中文出现在源文本里           -> 保留
  推断   只查到**部分**词根有依据（如 `white ceiling` 的 white）-> 保留
  未知   **反查不到**（库里没这个词）          -> **保留**
         很多新造复合词（golden mechanical dog）库里没有，
         因为查不到就删，会把好东西误杀 —— 宁可漏，不可错。
  未接地 **整标签**反查到了中文，源文本里一个字都对不上 -> 这才是编造
         （只查到部分词根，一律不算编造 —— 新造复合词会被误杀）
"""
import os
import re
import sys

# 同 nodes.py：ComfyUI 把插件当包导入，本目录不在 sys.path 上，
# 不补这一句 `from glossary import load` 会 ImportError（静默失效）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)          # insert(0)：保证加载的是本目录那份

from glossary import load


def _norm(s):
    return re.sub(r"\s+", " ", str(s).replace("_", " ")).strip().lower()


_STOP = {"a", "an", "the", "of", "and", "or", "with", "in", "on", "at", "to",
         "from", "by", "for", "is", "are", "as", "her", "his", "their",
         "no", "not", "slightly", "fully"}

# 这些字太常见，单独命中不算依据（"白色"的"色"、"一个"的"个"…）
# 注：单字命中已从 _hit 里去掉，这里只服务于 _cand 的最长候选逻辑

# 结构性标签：源文本里永远不会写，但缺了构图就垮，一律当接地
_SAFE = {"1girl", "2girls", "3girls", "1boy", "2boys", "multiple girls",
         "multiple boys", "solo", "solo focus", "female", "no humans",
         "looking at viewer", "facing viewer", "upper body", "full body",
         "cowboy shot", "portrait"}

# **只查这三个块**。③动作 ⑤画风 不该查：
#   动作里 facing viewer / standing 是合理推断，源文本不会写；
#   画风（soft brushwork / realistic）是风格选择，本来就"无中生有"。
#   拿源文本去卡这两块，只会满屏误报（实测 画风 块 realistic 被判编造）。
BLOCKS = ("角色", "衣着", "环境")

# 情绪互斥（nodes.resolve_emotion）会按原文补标签，
# 但原文常用口语说法（「心里好一顿滋火」「眼泪止不住」），术语库反查不到
# -> 会被误报成可疑。这是**我们自己按原文补的**标签，必须全认。
# ⚠️ 必须跟 nodes.py 里 EMO_CONFLICT 的四个情绪组一一对应，
#    漏一组就会出现"自己补的标签被自己判为编造"
#    （踩过两次：先漏了 angry，又漏了 crying）。
_EMO_SRC = (r"滋火|恼火|发火|生气|愤怒|火大|气炸|怒气|恼怒|不爽|恨|"
            r"慵懒|懒散|慵倦|不屑|轻蔑|鄙夷|爱理不理|漫不经心|"
            r"惊慌|慌张|惊恐|慌乱|惊愕|震惊|吓到|惊呆|手足无措|"
            r"哭泣|流泪|眼泪|大哭|伤心|悲伤|难过|"
            r"害羞|羞涩|脸红|不好意思")
_EMO_OK = {"angry", "frustrated",          # 愤怒组
           "languid", "dismissive", "half-lidded eyes",   # 慵懒/不屑组
           "panicked", "surprised",        # 惊慌组
           "crying", "tears",              # 哭泣组
           "blush", "embarrassed"}         # 害羞组


def _reverse():
    """英文标签 -> 中文名。整标签优先，再补单字。"""
    g = load()
    if "_rev" not in g:
        rev, word = {}, {}
        for t in g["tags"]:
            k = _norm(t["en"])
            if k and k not in rev:
                rev[k] = t["zh"]
            if "_" not in t["en"] and " " not in t["en"] and len(t["zh"]) <= 6:
                word.setdefault(k, t["zh"])
        g["_rev"], g["_word"] = rev, word
    return g["_rev"], g["_word"]


def _cand(zh):
    """术语库里一个英文常对多个中文（`white ceiling` -> `白色/天花板`）。

    取**最长**的那个当判据：长的一般是中心词。
    ⚠️ 不这么干会把 `white ceiling` 判成接地 —— 因为「白色」的「白」
    在源文本的「白衬衫」里出现过（踩过）。
    """
    if not zh:
        return ""
    parts = [x.strip() for x in str(zh).split("/") if x.strip()]
    return max(parts, key=len) if parts else ""


def _hit(zh, src, loose=False):
    """中文名在源文本里有没有依据。

    ⚠️ 别用「2 字窗口」当唯一门槛（踩过）：`湿皮肤` 对不上「头发是湿的」，
    `女孩` 对不上「少女」，结果把 `wet skin` / `1girl` 判成编造删掉了。
    ⚠️ 但**更不能用"任一单字命中"**（踩过）：那样 `乡下` 的「下」撞上「月下」、
    `花园` 的「花」撞上「樱花」，`rural garden` / `distant trees`
    这些编造全被判成"接地"漏掉。
    折中三档：
      整词命中         -> 接地
      任一 2 字窗口命中 -> 接地
      loose 且首字命中  -> 接地（**只给整标签用**：`月光` vs 原文「月下」）
    **词根路径一律严格** —— 编造正是在词根那里拼出来的。
    """
    zh = _cand(zh)
    if not zh:
        return False
    if zh in src:
        return True
    if any(zh[i:i + 2] in src for i in range(len(zh) - 1)):
        return True
    return bool(loose and len(zh) >= 2 and zh[0] in src)


def classify(tag, src):
    """返回 (级别, 反查到的中文, 是否**硬**证据)。

    第三位 hard 决定能不能删：
      hard=True   整标签在库里查到中文、源里却一个字都对不上 -> 真编造，可删
      hard=False  靠拆分词根推出来的 -> **只提示，永不删**
    理由：`standing` `serene` `soft fabric` `moonlight glow` 这类
    合理推断会被词根路径判成"未接地"（实测），删掉就是砍掉正常输出。
    """
    k = _norm(tag)
    # 结构性标签：成对出现的构图词，源文本里永远不会写，但绝不能删
    if k in _SAFE:
        return "接地", "结构性标签", False
    # 情绪互斥按原文补进来的标签（原文是口语说法，库里查不到）
    if k in _EMO_OK and re.search(_EMO_SRC, src):
        return "接地", "情绪线索", False
    rev, word = _reverse()
    zh = rev.get(k)
    if zh and _hit(zh, src, loose=True):
        return "接地", zh, False

    # 复合标签按词根拆开查。
    # ⚠️ 词根只用来**救**标签（接地/推断），**永远不用来定罪** ——
    #    `golden mechanical dog` 只查到 dog，源里没有狗，但它是新造复合词，
    #    删掉就是误杀（这是本模块自己写在开头的红线）。
    # ⚠️ 也不能"命中数≥未命中数就算接地"（踩过）：`white ceiling` 拆成
    #    white(白✅) + ceiling(天花板❌)，1 比 1 被判成接地，
    #    可"天花板"恰恰是编出来的那一半。只有**全部**词根都有依据才算接地。
    parts = [w for w in k.split(" ") if w and w not in _STOP]
    hit_parts, seen = 0, []
    for w in parts:
        z = word.get(w) or rev.get(w)
        if not z:
            continue
        seen.append(z)
        if _hit(z, src):
            hit_parts += 1
    if seen and hit_parts == len(seen):
        return "接地", zh or "/".join(seen), False
    if hit_parts:
        return "推断", zh or "/".join(seen), False
    # 整标签查到了中文、源里一个字都对不上 -> 这才是**硬**编造
    if zh:
        return "未接地", zh, True
    # ⚠️ 关键分支：**每个实词都能在库里查到，却一个都没命中** ——
    #    说明模型是拿已知词汇拼了个源文本里没有的新短语。
    #    实测漏网的 `rural garden` `gentle breeze` `white ceiling` 落这一支。
    #    但它也可能只是合理推断，所以 hard=False：只提示，不删。
    if seen and len(seen) == len(parts):
        return "未接地", "/".join(seen), False
    # 有实词查不到 —— **不判为编造**，保留
    return "未知", "/".join(seen), False


def check(tags, src, drop_ungrounded=False):
    """返回 (保留的标签, 报告字典)。"""
    rep = {"接地": [], "推断": [], "未知": [], "未接地": []}
    keep = []
    for t in tags:
        lvl, zh, hard = classify(t, src)
        if lvl == "未接地" and not hard:
            zh = (zh + " 仅提示") if zh else "仅提示"
        rep[lvl].append("%s(%s)" % (t, zh) if zh else t)
        # 只有**硬**证据才允许删；词根推出来的一律保留
        if lvl == "未接地" and hard and drop_ungrounded:
            continue
        keep.append(t)
    return keep, rep


def report(rep):
    """把报告拼成给「中文注释」框看的一段。"""
    n = sum(len(v) for v in rep.values())
    if not n:
        return ""
    lines = ["【接地检查】接地 %d / 推断 %d / 未知 %d / **未接地 %d**"
             % (len(rep["接地"]), len(rep["推断"]), len(rep["未知"]),
                len(rep["未接地"]))]
    if rep["未接地"]:
        lines.append("  可疑（源文本里找不到依据，可能是编的，也可能是合理推断）：")
        for x in rep["未接地"][:20]:
            # 整标签命中 -> 最可疑；词根推出来的 -> 仅供参考
            note = "　← 整标签命中，最可疑" if "仅提示" not in x else "　（词根推断，仅供参考）"
            lines.append("    · %s%s" % (x, note))
    return "\n".join(lines)
