# -*- coding: utf-8 -*-
"""Anima Prompt Writer —— 中文描述 -> 五块英文提示词。

设计要点（对应需求）：
  · 只在这一个节点里跑本地 LLM；**跑完就结束子进程，显存立刻释放**（offload）
  · 模型放 ComfyUI 的 models/LLM/ 下，节点自动扫描，不写死路径
  · 输出框只放英文；中文解释走前端单独一块区域（见 js/）
  · 支持「块输出」（五个框）与「整体输出」（一个框），由 overall_mode 控制
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

# ⚠️ **必须把本目录加进 sys.path**（踩过大坑）。
# ComfyUI 是把插件当**包**导入的（__init__.py 里写的是 `from .nodes import …`），
# 所以插件目录**不在 sys.path 上** —— 于是本文件里的 `import glossary` /
# `import grounding` 直接 ImportError，而且是**静默**的：
#     [AnimaPromptWriter] 术语库不可用: No module named 'glossary'
#     [AnimaPromptWriter] 回填跳过: No module named 'glossary'
#     [AnimaPromptWriter] 接地检查失败: No module named 'grounding'
# 后果是术语库提示、术语回填、接地检查在 ComfyUI 里**全部空转**，
# 而我在命令行测试时手动 insert 过插件目录，所以一直没暴露。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    # ⚠️ 用 insert(0) 而**不是** append：append 会把本目录排在最后，
    # 别的路径下只要有个同名 `glossary.py` / `grounding.py` 就会抢先加载
    # （实测真的加载到了别处的一份**同名旧副本** `grounding.py`，
    #  它连 `check()` 都没有，于是接地检查整条报 AttributeError）。
    sys.path.insert(0, _HERE)

# --- llama.cpp 运行时位置：优先环境变量，其次常见路径 ---
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))


def _local_llama_dir():
    """读插件目录下的 `llama_dir.txt`（本机专用，已 gitignore）。

    为什么要有这个：把本机路径写死在源码里，别人 clone 下来会跑不起来；
    但只靠环境变量，改完又得重启 ComfyUI 才生效（而且建目录联接需要管理员）。
    所以留一个**不入库**的小文件，优先级排在环境变量之后、bin/ 之前。
    文件内容就是一行 llama.cpp 目录的绝对路径。
    """
    try:
        p = os.path.join(_HERE_DIR, "llama_dir.txt")
        if os.path.exists(p):
            d = open(p, encoding="utf-8-sig").read().strip().strip('"')
            if d and os.path.isdir(d):
                return d
    except Exception:
        pass
    return ""


LLAMA_DIRS = [
    os.environ.get("LLAMA_CPP_DIR", ""),              # 推荐：环境变量
    _local_llama_dir(),                               # 或插件目录下的 llama_dir.txt
    os.path.join(_HERE_DIR, "bin"),                   # 或放本插件下的 bin/
    os.path.join(os.getcwd(), "llama.cpp"),           # 或 ComfyUI 根目录下的 llama.cpp/
]
# 不要把本机路径写死在这里 —— 别人 clone 下来会直接跑不起来。

BLOCK_DEF = """① 角色 —— 这个人是什么样：人数、性别、发型发色瞳色、兽耳兽尾、体型、皮肤、情绪神情、身体上的伤痕、整体的湿身状态
② 衣着 —— 身上穿的东西及其状态：服装鞋袜配饰、材质、颜色、破损/移位/撑开、裸露部位、走光结果
③ 动作 —— 她在做什么、在哪儿、什么姿态：站坐跪躺、手势、视线、正在做的事、位置关系、身上滴水的动态
④ 环境 —— 画面里的世界：场所、景物、天气、光线、时间、道具、镜头景别视角
⑤ 画风 —— 画面是怎么画出来的：媒介质感、笔触、上色、色调、光学效果、氛围基调。**由你根据画面内容与情绪自行决定**"""

SYSTEM = """你是动漫图像提示词生成器。把用户的中文画面描述，改写成五块结构化英文提示词。

输出格式（严格照做）：
① 角色
<英文标签，逗号加空格分隔> — <一句英文画面描述>
中文解释：<中文短语，逗号分隔>
② 衣着 / ③ 动作 / ④ 环境 / ⑤ 画风 —— 每块都是同样的三行结构。

分块定义：
%s

规则：
1 只写用户描述里真的有的东西。没提发色就不写发色，没提身材就不写身材，没提穿什么就不写具体服装。宁可少写，绝不编造。
2 不要照抄示例内容。示例只示范格式。示例里的地点、服装、人物、天气、道具，除非用户也提到，否则不得出现。
3 标签用**空格分隔的自然英文短句**，**不要用下划线**。
  例：写 `holding a holo umbrella`，别写 `holding_a_holo_umbrella`。
  原因：anima 的文本编码器是 Qwen3（语言模型），吃自然语言 ——
  实测同一套词，下划线版出图把画面里的"狗"整个丢了，空格版狗就出来了。
4 每块英文叙事把标签串成一句有画面感的话，不超过 14 个英文词。中文解释不超过 3 个短语。
5 五块都要有内容。评价性、氛围性的说法放进⑤画风当氛围词。
6 同一个标签只能出现在一个块里。①不写服装（归②）、不写正在做的动作（归③）。
7 写的是画面定格那一刻的状态，不是之前发生过什么。
8 只输出上述格式。不要前言、结语、思考过程、代码块标记。中文解释必须独立成行。
9 如果输入里没有人物（只描写物品、机械、场景），①角色 只写 no_humans，②衣着 整块省略，绝不许自己编一个人物出来。
10 禁止一切年龄段与幼态标签（loli/child/kid/mid-teen/teen/petite/flat_chest/young 等）。人物一律按成年女性写。
11 ①角色只写「这个人长什么样、什么状态」。服装归②、动作归③、场所归④。
12 **姿态必须自洽**。同一张图里不可能同时成立的标签，只能保留一个：
  · `hanging_from_chandelier`/`hanging_upside_down` 不能再配 `standing`/`arms_outstretched`/
    `on_the_ground`/`one_foot_on_ground`
  · `holding_xxx`（手里拿东西）不能再配 `arms_outstretched`/`arms_spread`
  · `lying`/`sitting`/`standing`/`hanging` 只能留一个
  实测踩过：输入「倒挂在吊灯上、手里拿电视」，输出同时给了 `arms_outstretched` +
  `hanging_from_chandelier` + `holding_a_tv`，三者互斥，出图只有第一个生效，另两个全废。
13 **翻译要贴原意**：拖鞋是 `slippers` 不是 `sneakers`；不要换成相近但不同的词。
14 **修饰词必须保留 —— 这是最容易丢、也最伤画面的东西。**
  原文里名词带修饰，就要连着修饰一起写：
  · 「金色机械小狗」→ `golden mechanical dog`，**不要只写 `dog`**
  · 「破旧泰迪熊」→ `worn teddy bear`
  · 「巨大的全息透明伞」→ `giant holo transparent umbrella`
  · 「发光雨衣」→ `glowing raincoat`
  只写中心词，画面就变成别的东西了。
15 每块尽量给足（理想 13~17 个），**但一切以原文信息为准，绝不为了凑数编造**。
  原文够写 15 个就写 15 个，只够写 6 个就写 6 个 —— **不够就少写，不许编。**
  特别禁止下面这类**凑数虚词**，它们不改变画面，只会稀释提示词：
  `steady posture` `calm stance` `emotionally composed` `gentle aura`
  `elegant vibe` `serene presence` `composed demeanor` `calm mood`
  `steady gaze` `quiet strength` —— 一律不许出现。
  每写一个标签，问自己：**原文里哪个词对应它？** 找不到就别写。""" % BLOCK_DEF

EXAMPLE_IN = ("崩铁角色，绯英，原皮，在河边骑着自行车，晴天，土路、小石子，湿身，"
              "水顺着衣角、袖口等末端滴落，情绪低落，狐狸耳朵下垂着，"
              "刚刚被小石子绊倒连车带人摔到河里，丝袜被树枝钩破，"
              "衣服在挣扎的时候被撑开了，导致胸露在了外面，还好有胸罩撑着，"
              "所幸没走光，但是乳沟是真的深，上半球完全露在外面，裙子也碎了，"
              "这个大腿根都露在外面了，光着肩膀，肩上还有摔跤留下的创口。画风你根据内容调整")

EXAMPLE_OUT = """① 角色
1girl, solo, fox ears, fox tail, long hair, wet hair, dripping hair, wet skin, pale skin, sad expression, ears down, drooping ears, bare shoulders, shoulder wound, bruise — A fox-eared girl, drenched and alone, her ears drooping in quiet sadness, a fresh wound on her bare shoulder.
中文解释：狐耳少女，长发湿透滴水，皮肤湿冷苍白，神情低落，耳朵下垂，肩部裸露带伤。
② 衣着
wet clothes, torn clothes, ripped skirt, torn stockings, stockings snagged, bra, bra visible, deep cleavage, upper breasts exposed, bare chest, bare shoulders, bare thighs, wet fabric, wet sleeves, cuffs, disheveled clothes — Her soaked outfit is torn open: the top ripped wide to show her bra and deep cleavage, the skirt shredded at the thigh, stockings snagged on a branch.
中文解释：衣着湿透破损，上衣撑开露出胸罩与深邃乳沟，上半球外露，裙子碎裂，丝袜钩破，大腿根与肩部裸露。
③ 动作
standing, beside river, bicycle fallen, fallen over, looking down, head down, dripping wet, water droplets, water dripping from sleeves, hand on wound, hand on shoulder, wet hair dripping, low spirits, after the fall — She stands by the riverbank, her bicycle fallen on the gravel, water dripping from her sleeves as she looks down, one hand on her wounded shoulder.
中文解释：站在河边，自行车倒地，低头，全身滴水，一手触肩伤，情绪低落。
④ 环境
sunny day, bright sunlight, clear sky, blue sky, dirt path, gravel, small stones, river, riverbank, outdoors, countryside, rural road, daylight, roadside grass, distant trees — A bright sunny day on a rural dirt road strewn with small stones, beside a gently flowing river under an open blue sky.
中文解释：晴天，阳光明亮，蓝天白云，土路布满小石子，河边，户外乡村，路旁有草。
⑤ 画风
painterly, soft lighting, wet look, realistic textures, atmospheric, muted palette, warm earthy tones, gentle shading, detailed rendering, soft shadows, melancholic mood, cinematic composition, subtle grain — Painterly style with soft lighting, realistic wet textures and a muted earthy palette, composed to match the melancholy mood.
中文解释：厚涂风格，柔和光照，湿润写实质感，低饱和大地色调，柔和阴影，忧郁氛围。"""

BL = ["角色", "衣着", "动作", "环境", "画风"]
SYM = {"①": "角色", "②": "衣着", "③": "动作", "④": "环境", "⑤": "画风"}
PLACEHOLDER = {"empty", "none", "n/a", "na", "null", "无", "-", "—", "nothing"}
BAN = re.compile(r"loli|child|kid|mid-teen|mid_teen|\bteen\b|petite|flat_chest|"
                 r"\byoung\b|toddler|infant|baby|shota")

# **凑数虚词**：不改变画面、只稀释提示词，模型在"要写满 N 个"的压力下会大量产出。
# 实测出现过 `steady posture` `calm stance` `emotionally composed`。
# 提示词第 15 条已明确禁止，这里再做一道确定性过滤。
FILLER = re.compile(
    r"^(steady|calm|composed|gentle|elegant|graceful|serene|quiet|soft|subtle)\s+"
    r"(posture|stance|aura|vibe|presence|demeanor|composure|mood|gaze|strength|"
    r"expression of calm|atmosphere)$"
    r"|^emotionally\s+\w+$|^(well[ -]?)?(balanced|harmonious)\s+\w+$")

# 姿态互斥表：同一张图里不可能同时成立。
# 实测踩过：输入「倒挂在吊灯上、手里拿电视」，输出同时给了
# `arms_outstretched` + `hanging_from_chandelier` + `holding_a_tv`，
# 三者互斥，出图时只有 `arms_outstretched` 生效，另外两个全废。
# 处理：**先出现的留下，后出现的丢掉**（模型通常把原文重点写在前面）。
# 开头的"穿着/套着 + 冠词"是叙述腔，不是标签本体。
# `wearing a waist apron` 和 `waist apron` 是**不同字符串**，精确去重抓不到它们 ——
# 实测输出里围裙出现三个变体：wearing a waist apron / waist apron / clean apron。
# 剥掉前缀后前两个会合并成同一条。**只剥这几个词**，不动 holding 这类有意义的动词。
_LEAD = re.compile(r"^(?:wearing|dressed in|clad in|put on)\s+(?:(?:a|an|the)\s+)?", re.I)
_ART = re.compile(r"^(?:a|an|the)\s+", re.I)


def _strip_lead(t):
    """剥掉开头的叙述性前缀；全剥没了就退回原样。"""
    out = _LEAD.sub("", t).strip()
    out = _ART.sub("", out).strip()
    return out or t


POSE_CONFLICT = [
    # 注意用**宽键**：曾经写 `sneaker_on_ground`，而实际标签是
    # `one_sneaker_still_on_ground`，子串没匹配上，漏掉了。
    (("hanging",), ("standing", "on_ground", "on_the_ground", "foot_on_ground")),
    (("hanging",), ("arms_outstretched", "arms_spread", "arms_extended")),
    # 手上有东西（拿着/抓着/攥着）时，手臂不可能同时往外张开。
    # ⚠️ 实测漏过：原表只有 holding/carrying，抓不到 `hands gripping hair`，
    #    于是 `arms outstretched` 和 `hands gripping hair` 一直同时出现。
    (("holding", "carrying", "gripping", "grabbing", "clutching",
      "holding_out", "reaching"),
     ("arms_outstretched", "arms_spread", "arms_extended")),
    (("lying",), ("standing",)),
    (("sitting",), ("standing", "on_ground")),
]


def _hit_any(tag, keys):
    # ⚠️ keys 用的是**下划线**写法（`arms_outstretched`），而标签早就被统一成
    #    **空格**了（下划线会让 anima 丢内容）。不做这个转换，互斥表就
    #    **永远匹配不上** —— 实测 `arms outstretched` 和 `hands gripping hair`
    #    一直同时出现在同一段提示词里，就是这处静默失效造成的。
    t = _nz(tag).replace(" ", "_")
    return any(k in t for k in keys)


def resolve_pose(tags):
    """把互斥的姿态标签按「先出现的留下」清一遍。"""
    kept, dropped = [], []
    for t in tags:
        bad = False
        for grp_a, grp_b in POSE_CONFLICT:
            for k in kept:
                if ((_hit_any(t, grp_a) and _hit_any(k, grp_b)) or
                        (_hit_any(t, grp_b) and _hit_any(k, grp_a))):
                    bad = True
                    break
            if bad:
                break
        (dropped if bad else kept).append(t)
    return kept, dropped


# --------------------------------------------------------------------------- #
# 本地推理：按需起 llama-server，用完立刻结束子进程（显存释放）
# --------------------------------------------------------------------------- #
def find_llama():
    for d in LLAMA_DIRS:
        if d and os.path.exists(os.path.join(d, "llama-server.exe")):
            return os.path.join(d, "llama-server.exe")
    return None


def find_model():
    """在 ComfyUI 的 models 下找 gguf。

    三条路，按可靠性排序：
      1. `folder_paths`（ComfyUI 内可用）
      2. **从本文件路径反推**：custom_nodes/<本插件>/nodes.py -> ../../models
         —— 这条路在 ComfyUI 内外都能用，冒烟测试就靠它
      3. 环境变量 COMFY_MODELS
    """
    roots = []
    try:
        import folder_paths
        for key in ("LLM", "llm", "text_encoders"):
            try:
                roots += list(folder_paths.get_folder_paths(key))
            except Exception:
                pass
        roots.append(folder_paths.models_dir)
    except Exception:
        pass
    # 反推：本文件在 <ComfyUI>/custom_nodes/<插件>/nodes.py
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.normpath(os.path.join(here, "..", "..", "models")))
    roots.append(os.path.join(here, "models"))
    if os.environ.get("COMFY_MODELS"):
        roots.append(os.environ["COMFY_MODELS"])

    cands = []
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for dp, _dn, fn in os.walk(r):
            for f in fn:
                if f.lower().endswith(".gguf"):
                    cands.append(os.path.join(dp, f))
    if not cands:
        env = os.environ.get("ANIMA_LLM_GGUF", "")
        return env if env and os.path.exists(env) else None
    # 多个 GGUF 时挑一个：优先 Qwen3，其次文件大的（量化更高、通常更准）。
    # 可用环境变量 ANIMA_LLM_GGUF 直接指定，跳过这里的挑选。
    cands.sort(key=lambda p: (("qwen3" not in os.path.basename(p).lower(),
                               -os.path.getsize(p))))
    return cands[0]


def free_vram_mb():
    """当前空闲显存（MB）。拿不到就返回 None，交给调用方走保守策略。"""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return int(out.strip().split("\n")[0])
    except Exception:
        return None


class _Server:
    """一次性的 llama-server：起 -> 用 -> 杀。

    **显存是这个节点的第一约束**：出图要用卡，所以：
      · 起之前先量空闲显存，不够就退到 CPU（`-ngl 0`），绝不跟出图抢
      · 用完必须把进程真正杀掉，并**等到显存回到起之前的水位**才返回
    """

    def __init__(self, exe, model, port=8099, ngl=99, ctx=4096):
        self.exe, self.model, self.port = exe, model, port
        self.ngl, self.ctx = ngl, ctx
        self.proc = None
        self.vram_before = None

    def __enter__(self):
        if not self.exe or not self.model:
            raise RuntimeError("找不到 llama-server.exe 或 gguf 模型")
        self.vram_before = free_vram_mb()
        self.proc = subprocess.Popen(
            [self.exe, "-m", self.model, "-ngl", str(self.ngl),
             "-c", str(self.ctx), "-t", "6", "-fa", "on", "-np", "1",
             "--host", "127.0.0.1", "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = "http://127.0.0.1:%d/health" % self.port
        for _ in range(180):
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server 启动即退出（显存不够？试纯CPU档）")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return self
            except Exception:
                time.sleep(0.5)
        self.__exit__(None, None, None)
        raise RuntimeError("llama-server 启动超时")

    def __exit__(self, *a):
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass
        # 等显存回到起之前的水位 —— 不等就返回，出图那边可能拿到一张被占着的卡
        if self.vram_before is not None:
            for _ in range(40):                      # 最多等 20 秒
                now = free_vram_mb()
                if now is None or now >= self.vram_before - 64:
                    break
                time.sleep(0.5)

    def complete(self, prompt, n_predict=900, temp=0.3, timeout=300):
        body = json.dumps({"prompt": prompt, "n_predict": n_predict,
                           "temperature": temp, "top_p": 0.9,
                           "repeat_penalty": 1.05, "stream": False,
                           "stop": ["<|im_end|>", "<|im_start|>"],
                           "cache_prompt": True}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/completion" % self.port,
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("content", "")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def parse_request(text):
    mode, blocks, desc = "丰富", None, text
    m = re.search(r"模式\s*[:：]\s*(丰富|简约)", text)
    if m:
        mode = m.group(1)
    m = re.search(r"块\s*[:：]\s*([①②③④⑤\s]+)", text)
    if m:
        sel = [b for b in "①②③④⑤" if b in m.group(1)]
        if sel and len(sel) < 5:
            blocks = sel
    m = re.search(r"描述\s*[:：]\s*(.*)$", text, re.S)
    if m:
        desc = m.group(1)
    return mode, blocks, desc.strip()


def glossary_hint(text):
    """查术语库，把命中的标准标签拼成一段「参考」塞给模型。

    为什么需要：模型不可能背下 3 万条标签 —— 用户说「晚礼服」，它可能自己造词。
    但**检索不费算力也不费显存**，查出来让它照抄就行（RAG，不用训练）。
    术语库随插件走（同目录 glossary.json），插件自包含。
    """
    try:
        import glossary as G
        # top 必须 ≥ render 的分组上限，否则后面的词在检索阶段就被截掉了
        hits, chars = G.retrieve(text, top=30, chars_top=2)
        return G.render(hits, chars)
    except Exception as e:
        print("[AnimaPromptWriter] 术语库不可用: %s" % e)
        return ""


def _nz(s):
    """统一成小写空格分隔，用于比对标签是否"已经表达过了"。"""
    return re.sub(r"\s+", " ", str(s).replace("_", " ")).strip().lower()


# 情绪互斥：原文明确写了情绪，输出却给了反向情绪。
# 实测：原文「心里好一顿滋火却又无能为力，狠狠的握着」，
#       模型输出 `calm expression`（平静）—— 语义整个反了。
# 这类"反向情绪"必须靠代码兜，光在提示词里写约束治不了。
# 句子式标签：不是标签，是一句描述。对出图几乎没有驱动作用，
# 还占着位置。实测：「深深的无力感和强烈的创作欲」被译成
# `deep sense of exhaustion and energy` —— 这不是能画的标签。
_SENTENCE = re.compile(r"\b(?:sense|feeling|air|aura|expression)\s+of\b", re.I)
# 否定式：`not choked` / `no hat`。**只在同现肯定式时才删** ——
# `no tears or rips`（衣服完好）是有用的否定式，没有对应肯定式，必须留。
_NEG = re.compile(r"^(?:not|no)\s+(.+)$", re.I)


def _flat(p):
    """按 BL 顺序把各块标签拉平，保留块名和块内先后。"""
    out = []
    for k in BL:
        v = p.get(k)
        if v:
            for t in v["tags"]:
                out.append((k, t))
    return out


def resolve_contradiction(p, max_words=9):
    """清三类垃圾/冲突标签（跨块，先出现的留下）。

    1 句子式：含 `sense of` / `feeling of`，或词数 ≥ max_words
    2 否定式与肯定式同现：`not choked` + `choke`
      —— 实测模型写 `not choked`，术语回填又补 `choke`，凑成自相矛盾
    3 跨块互斥姿态：②的 `arms outstretched` + ③的 `hands gripping hair`
      —— 手不可能既往外伸又抓头发
    """
    flat = _flat(p)
    pos = {_nz(t) for _k, t in flat}
    dropped, kept = [], []
    for blk, t in flat:
        n = _nz(t)
        if _SENTENCE.search(t) or len(t.split()) >= max_words:
            dropped.append("%s(句子式)" % t)
            continue
        m = _NEG.match(n)
        # 前 4 个字母比一下就够：`choked` 和 `choke` 都是 `chok`
        if m and any(m.group(1)[:4] == q[:4] for q in pos if q != n):
            dropped.append("%s(否定式冲突)" % t)
            continue
        bad = None
        for ga, gb in POSE_CONFLICT:
            for idx, (kb, kt) in enumerate(kept):
                if ((_hit_any(t, ga) and _hit_any(kt, gb)) or
                        (_hit_any(t, gb) and _hit_any(kt, ga))):
                    bad = (idx, kb, kt)
                    break
            if bad:
                break
        if bad:
            idx, kb, kt = bad
            # ⚠️ 姿态标签属于③动作，冲突时**优先保留动作块里那条**。
            #    实测：`arms outstretched` 被模型错放进②衣着，
            #    `hands gripping hair` 在③动作且与原文一致 ——
            #    单纯"先出现的留下"会把**错的那条**留下来。
            if blk == "动作" and kb != "动作":
                dropped.append("%s(姿态互斥，让位给动作块)" % kt)
                kept.pop(idx)
                kept.append((blk, t))
                continue
            dropped.append("%s(姿态互斥)" % t)
            continue
        kept.append((blk, t))
    if not dropped:
        return p
    for k in BL:
        if k in p:
            p[k]["tags"] = [t for b, t in kept if b == k]
    for k in [k for k in list(p) if not p[k]["tags"]]:
        del p[k]
    print("[AnimaPromptWriter] 冲突/垃圾标签，丢弃: %s" % ", ".join(dropped))
    return p


EMO_CONFLICT = [
    # (原文里的情绪线索, 必须表达, 必须删掉的反向标签)
    (r"滋火|恼火|发火|生气|愤怒|火大|气炸|怒气|恼怒|不爽|恨",
     ["angry", "frustrated"],
     r"^calm|serene|smiling|smile|peaceful|content|gentle expression|"
     r"relaxed|tranquil|happy|cheerful"),
    # ⚠️ 惊慌/慌张/惊恐 术语库里**没有**（实测），所以模型遇到它们会自由发挥成
    #    `calm but slightly distracted` —— 语义完全反了。必须代码兜。
    # ⚠️ 这一组有**出图证据**：原文「神情慵懒且带着一丝不屑」，
    #    输出是 `calm expression, slightly dismissive, serene`，
    #    画出来是**平静略带忧郁**，慵懒和不屑都没到 ——
    #    `calm`/`serene` 把唯一正确的 `dismissive` 淹掉了。
    #    而 `serene` 正是接地检查标为"源文本里找不到依据"的那一条：
    #    那条多余标签在实际出图上是**负作用**。
    (r"慵懒|懒散|慵倦|不屑|轻蔑|鄙夷|爱理不理|漫不经心",
     ["languid", "dismissive", "half-lidded eyes"],
     r"^calm|serene|smiling|smile|peaceful|content|composed|cheerful|"
     r"happy|relaxed|gentle expression|tranquil"),
    (r"惊慌|慌张|惊恐|慌乱|惊愕|震惊|吓到|惊呆|手足无措",
     ["panicked", "surprised"],
     r"^calm|serene|smiling|smile|peaceful|content|composed|"
     r"relaxed|tranquil|happy|cheerful|focused"),
    (r"哭泣|流泪|眼泪|大哭|伤心|悲伤|难过",
     ["crying", "tears"],
     r"^calm|smiling|smile|happy|cheerful|serene"),
    (r"害羞|羞涩|脸红|不好意思",
     ["blush", "embarrassed"],
     r"^calm|serene|confident"),
]


def resolve_emotion(tags, src):
    """按原文的情绪线索，删掉反向情绪标签并补上正确情绪。

    返回 (新标签, 被删的, 被加的)。**只在①角色块用**（情绪标签都在那）。
    """
    if not src or not tags:
        return tags, [], []
    drop_re, want = None, []
    for pat, need, bad in EMO_CONFLICT:
        if re.search(pat, src):
            drop_re, want = re.compile(bad, re.I), need
            break
    if drop_re is None:
        return tags, [], []
    keep = [t for t in tags if not drop_re.search(t.strip())]
    dropped = [t for t in tags if drop_re.search(t.strip())]
    joined = " ".join(_nz(t) for t in keep)
    added = []
    for w in want:
        if len(added) >= 2:              # 最多补 2 个：既要"慵懒"也要"不屑"
            break
        if w not in joined:
            keep.append(w)
            added.append(w)
    return keep, dropped, added


def backfill(p, src, max_add=12):
    """保障性回填：检索命中了、但模型漏掉的术语，直接补进对应块。

    为什么需要：术语库命中已经塞进提示词了，模型**仍然**会漏抄。
    实测丢过的：`乳沟`(cleavage)、`汉服`(hanfu)、`乳贴`(pasties)、`渔网`(fishnet)。
    这些是**确定性的词汇映射**，不需要模型来判断 —— 直接补，比调提示词可靠。

    只补术语库里**确认命中**的词（即原文里真出现过），所以不是在编造。
    """
    try:
        import glossary as G
        hits, chars = G.retrieve(src, top=30, chars_top=2)
    except Exception as e:
        print("[AnimaPromptWriter] 回填跳过: %s" % e)
        return p, []

    have = set()
    for v in p.values():
        for t in v["tags"]:
            have |= set(_nz(t).split(" "))
    added, done, done_zh = [], set(), set()

    # **角色也要回填**。实测 `刻晴` 在库里（keqing (genshin impact)，7377 帖）、
    # 检索也命中了，模型依然丢掉 —— 用户明确打了角色名，就必须落进去。
    # 只补最好的那一个（retrieve 已按 posts 降序），避免把变体一起塞进去。
    if chars:
        ce = G._norm(chars[0]["en"])
        if ce and not set(ce.split(" ")) <= have:
            v = p.setdefault("角色", {"tags": [], "narr": "", "cn": ""})
            if not any(_nz(t) == ce.lower() for t in v["tags"]):
                v["tags"].append(ce)
                have |= set(ce.lower().split(" "))
                added.append("角色→%s" % ce)

    for h in hits:
        if len(added) >= max_add:
            # ⚠️ 截断必须**说出来**。`hits` 是按**原文出现顺序**排的，
            #    所以被截掉的永远是句子**后半段**的词 ——
            #    实测「她右手夹着…草莓冰淇淋，左手拿着…诗集」里，
            #    草莓/冰淇淋 排在后面，被前面 6 个吃满上限，
            #    出图时冰淇淋**整个没画出来**。
            #    同一个"按位置截断饿死尾部"的坑，本项目已经踩了三次
            #    （render() 分组上限、render() 12 组、这里），
            #    所以这次至少让它可见。
            print("[AnimaPromptWriter] **回填达到上限 %d（候选共 %d 条），"
                  "尾部术语可能被饿死**" % (max_add, len(hits)))
            break
        e = G._norm(h["en"])
        if not e or e.endswith("_") or e.lower() in done:
            continue
        # ⚠️ **一个中文只补一个写法**（踩过）：`阳光` 在库里有 sunshine /
        #    sunbeam / sun / sun rays 四个写法，每个 en 都不同，
        #    按 en 去重根本挡不住 —— 实测一口气补了 4 个，
        #    输出里出现 7 个太阳相关标签，而且接地检查反过来把 `sun`
        #    标成"最可疑"。**我自己补的东西被我自己的检查判为编造。**
        if h["zh"] in done_zh:
            continue
        toks = set(e.lower().split(" "))
        if not toks or toks <= have:
            continue                      # 输出里已经表达了，别重复加
        # ⚠️ 词干已经在输出里出现过就别加（踩过）：模型写了 `not choked`，
        #    库里 `窒息→choke` 又补了一个 `choke`，同一段提示词里出现
        #    **`not choked` 和 `choke` 自相矛盾**。
        #    用"前 4 个字母是子串"来兜 —— `chok` 在 `not choked` 里，就跳过。
        _stem = e.lower()[:min(4, len(e))]
        if any(_stem in _nz(t) for v0 in p.values() for t in v0["tags"]):
            continue
        blk = re.sub(r"^[①②③④⑤]\s*", "", str(h["block"])).strip()
        # ⚠️ 术语库里的块名带符号（`④环境`），而 p 的键是**纯块名**（`环境`）。
        #    不剥符号就会往幽灵块里加，pack() 只认 SYM 的值 -> **静默失效**。
        #    踩过：日志明明打了「回填 红色→red」，输出里却没有 red。
        # 空串 = 来源不可信（themes_v21.json 的块是猜的）。**宁可不补，
        # 也不能补到错的块里** —— 实测 `盘腿坐→indian style` 被标成⑤画风，
        # 补进去之后模型在画风块里写出了"印度风格构图"。
        if blk not in SYM.values():
            continue
        # 颜色词在库里挂在 background 下（block=④环境），但颜色是**属性**，
        # 回填进环境块没意义 —— 实测 `red` 被塞到了④环境。颜色一律进①角色。
        if str(h["zh"]).endswith("色"):
            blk = "角色"
        done.add(e.lower())
        done_zh.add(h["zh"])
        v = p.setdefault(blk, {"tags": [], "narr": "", "cn": ""})
        if any(_nz(t) == e.lower() for t in v["tags"]):
            continue
        v["tags"].append(e)
        have |= toks
        added.append("%s→%s" % (h["zh"], e))
    if added:
        print("[AnimaPromptWriter] 术语回填(模型漏抄): %s" % ", ".join(added))
    return p, added


# --------------------------------------------------------------------------- #
# 第一段：合理拓展中文
# ---------------------------------------------------------------------------
EXTEND_SYS = """把用户的中文画面描述**补充**成更完整的一段中文，供画师理解画面。

**核心原则：只补「必然有」的，绝不补「可能有」的。**

1 原文信息一个字都不能丢。
2 只补**这个场景必然包含**的东西。判断标准是「反过来想：不写它，画面就画不出来吗？」
   · 「教室」→ **必然有**课桌椅、黑板。可以补。
   · 「室内」→ 墙、窗、家具**不一定有**（可能是一间空房、一间牢房、一片纯色背景）。
     **不许补。**
   · 「少女穿便装站室内」→ 补「白衬衫、百褶裙」是**发明**，原文没说穿什么。**不许补。**
   · 「赛博都市天台」→ 补「霓虹灯牌、湿滑地面」可以说必然（赛博都市的标配）。
     补「机械小狗有四只轮子」是**发明细节**。**不许补。**
3 **禁止**补充的东西：
   · 原文没提的服装、发色、瞳色、身材
   · 原文没提的家具、墙面、窗户、地板
   · 原文没提的机械结构、道具细节
   · 情绪的心理描写（"透出安心与感激"）、声音描写（"钟鸣声"）
4 长度**最多是原文的 1.5 倍**，宁短勿长。补不出东西就不要硬补。
5 用中文写**一段话**。不要分点、不要小标题、不要解释。
6 只输出补充后的描述，别的什么都不要写。"""


def _call(srv, prompt, n_predict=900):
    return srv.complete(prompt)


def expand_zh(srv, desc):
    """第一段调用：中文 -> 拓展后的中文。失败就退回原文。"""
    p = ("<|im_start|>system\n%s<|im_end|>\n"
         "<|im_start|>user\n%s<|im_end|>\n"
         "<|im_start|>assistant\n<think>\n\n</think>\n\n" % (EXTEND_SYS, desc))
    try:
        out = srv.complete(p, n_predict=400, temp=0.4).strip()
    except Exception as e:
        print("[AnimaPromptWriter] 中文拓展失败，用原文: %s" % e)
        return desc
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    # 兜底：太短说明没拓展成功，直接用原文
    if len(out) < len(desc) * 0.9:
        return desc
    return out


def build_prompt(text, only=None, rich=False):
    """only 给了就只要求这几块（两轮生成用）。

    rich=True 时明确要求每块的标签数量 —— 单轮里五块一起出，
    模型会把标签写得很省；拆成两轮后每块预算多了，就该要更多。
    """
    mode, blocks, desc = parse_request(text)
    if only:
        blocks = only
    tail = []
    if blocks:
        tail.append("只输出这几块，其他块一个都不要出现：%s"
                    % "、".join("%s %s" % (b, SYM[b]) for b in blocks))
    if mode == "简约":
        tail.append("简约模式：每块标签不超过 4 个，叙事不超过 12 词，中文解释不超过 4 个短语。")
    elif rich:
        tail.append("每块给足 8~14 个英文标签，把描述里该有的细节都写出来，不要省。"
                    "英文叙事不超过 18 个词。中文解释不超过 5 个短语。")
    hint = glossary_hint(desc)
    q = desc + (("\n\n[输出要求] " + " ".join(tail)) if tail else "")
    if hint:
        q = hint + "\n\n[画面描述] " + q
    return ("<|im_start|>system\n%s<|im_end|>\n"
            "<|im_start|>user\n%s<|im_end|>\n"
            "<|im_start|>assistant\n%s<|im_end|>\n"
            "<|im_start|>user\n%s<|im_end|>\n"
            "<|im_start|>assistant\n" % (SYSTEM, EXAMPLE_IN, EXAMPLE_OUT, q))


def parse_out(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    out, cur = {}, None
    for ln in text.split("\n"):
        m = re.match(r"^\s*[①②③④⑤]\s*(角色|衣着|动作|环境|画风)\s*$", ln)
        if m:
            cur = m.group(1)
            out[cur] = {"tags": [], "narr": "", "cn": ""}
            continue
        if cur is None or not ln.strip():
            continue
        if re.match(r"^\s*中文\s*(解释|说明)", ln):
            out[cur]["cn"] = re.split(r"[:：]", ln, 1)[-1].strip()
        elif "—" in ln:
            a, b = ln.split("—", 1)
            out[cur]["tags"] = [t.strip() for t in a.split(",") if t.strip()]
            out[cur]["narr"] = b.strip()
        else:
            out[cur]["tags"] = [t.strip() for t in ln.split(",") if t.strip()]
    # ---- 归一化 + 过滤 + 跨块去重（**重复时优先保留衣着块的那份**）----
    # 为什么要两趟：同一条标签可能同时出现在①角色和②衣着
    #（实测 `wearing a waist apron` 在①、`waist apron` 在②），
    # 原来只按 BL 顺序留**先出现的**，于是围裙被留在了①角色里 —— 块是错的。
    try:
        import glossary as _G
    except Exception:
        _G = None

    # 第一趟：每块各自归一化、过滤（先不跨块去重）
    clean = {}
    for k in BL:
        v = out.get(k)
        if not v:
            continue
        keep = []
        for t in v["tags"]:
            # **把下划线换成空格** —— 这是本插件最重要的一处适配。
            # anima 的编码器是 Qwen3（语言模型），吃自然语言；
            # 实测下划线版 `holding_a_holo_umbrella` 出图丢了"狗"，空格版就出来了。
            t = t.replace("_", " ").strip()
            t = re.sub(r"\s+", " ", t)
            t = _strip_lead(t)            # `wearing a waist apron` -> `waist apron`
            if not t or t.lower() in PLACEHOLDER or BAN.search(t):
                continue
            if FILLER.match(t):
                print("[AnimaPromptWriter] 凑数虚词，丢弃: %s" % t)
                continue
            keep.append(t)
        clean[k] = keep

    # 第二趟：同一个去重键只留一个块。选块优先级：
    #   1 术语库认定的块（数据说话）—— `waist apron` 库里就是②衣着
    #   2 退而求其次：衣着块优先（用户指定）
    #   3 再不行按 BL 自然顺序取第一个
    where = {}
    for k in BL:
        for t in clean.get(k, []):
            where.setdefault(t.lower(), []).append(k)
    winner = {}
    for key, blocks in where.items():
        if len(blocks) == 1:
            winner[key] = blocks[0]
            continue
        want = None
        if _G is not None:
            try:
                want = _G.block_of(key)
            except Exception:
                want = None
        if want in blocks:
            winner[key] = want
        elif "衣着" in blocks:
            winner[key] = "衣着"
        else:
            winner[key] = blocks[0]
    for k in BL:
        v = out.get(k)
        if not v:
            continue
        v["tags"] = [t for t in clean.get(k, []) if winner.get(t.lower()) == k]
    # 姿态互斥：只在动作块里清理（姿态标签都在③）
    act = out.get("动作")
    if act and act["tags"]:
        act["tags"], dropped = resolve_pose(act["tags"])
        if dropped:
            print("[AnimaPromptWriter] 姿态互斥，丢弃: %s" % ", ".join(dropped))
    return {k: v for k, v in out.items() if v["tags"]}


def render_blocks(p):
    out = []
    for s in "①②③④⑤":
        k = SYM[s]
        v = p.get(k)
        if not v:
            continue
        line = ", ".join(v["tags"])
        if v["narr"]:
            line += " — " + v["narr"]
        out.append("%s %s\n%s" % (s, k, line))
    return "\n\n".join(out)


def render_overall(p):
    """整体输出：按块序把所有标签连成一条，给 CLIP 直接用。"""
    tags = []
    for s in "①②③④⑤":
        v = p.get(SYM[s])
        if v:
            tags += v["tags"]
    return ", ".join(tags)


def render_cn(p):
    """中文注释：给前端显示，不进输出框。"""
    return "\n".join("%s %s：%s" % (s, SYM[s], p[SYM[s]]["cn"])
                     for s in "①②③④⑤"
                     if SYM[s] in p and p[SYM[s]]["cn"])


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
class AnimaPromptWriter:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "描述": ("STRING", {"multiline": True, "default": "",
                                    "tooltip": "中文画面描述。也可直接写 模式:丰富|简约 | 描述:…"}),
                "模式": (["丰富", "简约"], {"default": "丰富"}),
                "输出方式": (["块输出", "整体输出"], {"default": "块输出"}),
                "分段": (["不分段(默认)", "分段两轮(更慢)"], {
                    "default": "不分段(默认)",
                    "tooltip": "分段=分两次调用（①②③ + ④⑤）。\n"
                               "实测：分段把 8.6 秒拖到 15.9 秒，**标签总数没变**（32→32），\n"
                               "所以默认单轮。"}),
                "接地检查": (["标记(不删)", "关闭"], {
                    "default": "标记(不删)",
                    "tooltip": "输出后逐个标签**反查术语库**，看它对应的中文在源文本里有没有依据，"
                               "结果写进「中文注释」。\n"
                               "堵的是**翻译阶段的编造**（把「室内」翻成 clean walls, white ceiling）。\n\n"
                               "⚠️ **只做提示，不删标签**。实测过一版「删除未接地」，它删掉的是\n"
                               "standing / serene / soft fabric 这类**合理推断**（「伸手接花瓣」\n"
                               "当然蕴含站立），真正该抓的 white ceiling / rural garden\n"
                               "反而漏了。拿源文本做接地，原理上就分不开推断和编造 —— \n"
                               "所以这个检查只当**提示**用，删不删你自己判断。\n\n"
                               "反查不到的标签一律保留（很多新造复合词库里没有）。"}),
                "用拓展中文": (["是(更丰富)", "否(更快)"], {
                    "default": "是(更丰富)",
                    "tooltip": "是＝先让模型把「描述」**补充**成更完整的中文，\n"
                               "再**用补充后的中文**翻译成五块英文。\n"
                               "补充结果同时写进「拓展框」，可以人工编辑。\n"
                               "否＝直接拿「描述」生成，更快但内容更少。\n\n"
                               "⚠️ 和「仅拓展中文」的分工（别混）：\n"
                               "  「用拓展中文」管**要不要拓展** —— 拓展完照常出 tags\n"
                               "  「仅拓展中文」管**拓完就停** —— 只出中文，不出 tags"}),
                "仅拓展中文": (["否(正常生成)", "是(只拓展不出图)"], {
                    "default": "否(正常生成)",
                    "tooltip": "**两步用法**：\n"
                               "1 先设成「是」并执行 —— 只出拓展后的中文，不生成 tags\n"
                               "2 在「拓展框」里删掉模型编多的句子\n"
                               "3 再设回「否」执行 —— 直接拿你改过的中文出 tags\n"
                               "好处：模型补充的每一句你都过目，编造不会溜进最终提示词。"}),
                "显存策略": (["自动", "全GPU", "纯CPU"], {
                    "default": "自动",
                    "tooltip": "自动=先量空闲显存，不够就退纯CPU，绝不跟出图抢；"
                               "全GPU=强制上卡（快，但可能与出图模型冲突）；"
                               "纯CPU=完全不占显存（慢，但绝对安全）"}),
                "启用": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                # 六个文本框：声明成节点输入才会渲染成真正的多行文本框。
                # ⚠️ 键名必须是**合法 Python 标识符**：`①`(U+2460) 不是，会直接语法错误。
                # 所以用 `框1角色` 这种（汉字 + 数字），显示上一样清楚。
                "框1角色": ("STRING", {"multiline": True, "default": "",
                                      "tooltip": "模型输出，可直接编辑。非空时不再跑模型。"}),
                "框2衣着": ("STRING", {"multiline": True, "default": ""}),
                "框3动作": ("STRING", {"multiline": True, "default": ""}),
                "框4环境": ("STRING", {"multiline": True, "default": ""}),
                "框5画风": ("STRING", {"multiline": True, "default": ""}),
                "整体框": ("STRING", {"multiline": True, "default": ""}),
                # 拓展中文单独一个框，**可编辑**：
                # 模型拓展完写进来，你看一眼觉得哪句编多了就删掉，
                # 下次执行会直接用你改过的版本（不再重新拓展）。
                "拓展框": ("STRING", {"multiline": True, "default": "",
                                     "tooltip": "第一段「补充中文」的结果，可直接编辑。\n"
                                                "非空时不再重新拓展，直接用你改过的。\n"
                                                "改「描述」会自动清空这里。"}),
                "中文注释": ("STRING", {"multiline": True, "default": ""}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",) * 6
    RETURN_NAMES = ("①角色", "②衣着", "③动作", "④环境", "⑤画风", "整体")
    FUNCTION = "write"
    CATEGORY = "Anima/提示词"
    DESCRIPTION = "中文描述 -> 五块英文提示词。本地 4B 模型，跑完即释放显存。"

    @classmethod
    def IS_CHANGED(cls, **kw):
        return float("nan")          # 每次都重跑（输入文字变了要重新生成）

    def write(self, 描述, 模式, 输出方式, 分段, 接地检查, 用拓展中文, 仅拓展中文,
              显存策略, 启用,
              框1角色="", 框2衣着="", 框3动作="", 框4环境="", 框5画风="",
              整体框="", 拓展框="", 中文注释="", unique_id=None):
        empty = ("",) * 6
        boxes = [框1角色, 框2衣着, 框3动作, 框4环境, 框5画风]

        # 接地检查：反查术语库，看标签在源文本里有没有依据。
        # 堵的是**翻译阶段的编造** —— 拓展框的编辑治不了这一层。
        # ⚠️ 必须放在**生成之后**：它要拿 p 和 src 做输入（踩过：写在前面
        #    引用了还不存在的 p / src，而且那时术语库比对的是错的键名）。
        gnd = ""

        def ground(p, src):
            """就地过滤 p 的标签。返回过滤后的 p；报告写进 gnd。"""
            nonlocal gnd
            if str(接地检查).startswith("关闭") or not p:
                return p
            if not src or not str(src).strip():
                return p          # 没有源文本可比，宁可不判
            try:
                import grounding as GD
                # ⚠️ 删除模式已**撤掉**（见 INPUT_TYPES 里「接地检查」的说明）：
                #    实测它会删掉 standing / serene / soft fabric 这类合理推断，
                #    而真正该抓的 white ceiling / rural garden 反倒漏掉。
                #    拿源文本做接地，原理上分不开"推断"和"编造" —— 只当提示用。
                drop = False
                # 只查 ①角色 ②衣着 ④环境。③动作/⑤画风 不该查 ——
                # 动作是合理推断、画风是风格选择，源文本里本来就不会写，
                # 硬查只会满屏误报（见 grounding.BLOCKS 注释）。
                # ⚠️ p 的键是**纯块名**（`角色`），不是 `①角色` ——
                # parse_out 里 cur = m.group(1)，SYM[s] 也返回 `角色`。
                for k in getattr(GD, "BLOCKS", ("角色", "衣着", "环境")):
                    v = p.get(k)
                    if not v:
                        continue
                    v["tags"], rep = GD.check(v["tags"], src, drop_ungrounded=drop)
                    gnd += "\n" + GD.report(rep)
                return {k: v for k, v in p.items() if v["tags"]}
            except Exception as e:
                print("[AnimaPromptWriter] 接地检查失败: %s" % e)
                return p

        def pack(p):
            """返回值 + 给前端的 ui 数据。中文注释走 ui，不占输出槽。"""
            blocks = [", ".join(p[SYM[s]]["tags"]) if SYM[s] in p else ""
                      for s in "①②③④⑤"]
            whole = render_overall(p)
            res = (whole,) * 6 if 输出方式 == "整体输出" else tuple(blocks) + (whole,)
            cn = "【各块吃进的中文】\n" + render_cn(p)
            if gnd:
                cn += "\n" + gnd
            return {"ui": {"text": list(res), "cn": [cn], "expanded": [expanded]},
                    "result": res}

        # 框里有内容 -> 直接采用（用户改过，或沿用上次结果），不再跑模型
        if any(b and b.strip() for b in boxes) or (整体框 and 整体框.strip()):
            # ⚠️ 必须拼成 `①角色` 这种「符号+块名」，只拼符号 `①` 正则不认（踩过）
            p = parse_out("\n".join(
                "%s%s\n%s" % (sym, SYM[sym], b)
                for sym, b in zip("①②③④⑤", boxes) if b and b.strip()))
            if p:
                return pack(ground(p, 描述))
            if 整体框 and 整体框.strip():
                return {"ui": {"text": [整体框] * 6, "cn": [""]},
                        "result": (整体框,) * 6}
            return {"ui": {"text": [""] * 6, "cn": [""]}, "result": empty}

        if not 启用 or not 描述.strip():
            return {"ui": {"text": [""] * 6, "cn": [""]}, "result": empty}
        exe, model = find_llama(), find_model()
        # 显存策略：出图要用卡，所以默认先量再决定，绝不硬抢
        ngl = 99
        if 显存策略 == "纯CPU":
            ngl = 0
        elif 显存策略 == "自动":
            free = free_vram_mb()
            # 4B Q4_K_M 约 2.3GB + KV/上下文约 0.6GB，留 1GB 余量
            if free is not None and free < 4000:
                ngl = 0
                print("[AnimaPromptWriter] 空闲显存 %d MB 不足，退纯 CPU（不占卡）" % free)
        two = str(分段).startswith("分段")
        do_expand = str(用拓展中文).startswith("是")
        only_expand = str(仅拓展中文).startswith("是")
        expanded = ""
        try:
            with _Server(exe, model, ngl=ngl) as srv:
                # 第一段：补充中文。
                # **拓展框非空就用它**（用户改过，或上次的结果），不再重新拓展 ——
                # 这样你可以在框里删掉编多的句子，然后直接生成。
                if 拓展框 and 拓展框.strip():
                    expanded = 拓展框.strip()
                elif do_expand:
                    expanded = expand_zh(srv, 描述)
                # **只拓展模式**：出完中文就收工，不生成 tags。
                # 用户先跑这一步 -> 人工删掉编多的句子 -> 再关掉开关出 tags。
                if only_expand:
                    note = "【拓展后的中文 —— 请删掉模型编多的句子，再关掉「仅拓展中文」执行】"
                    return {"ui": {"text": [""] * 6,
                                   "cn": ["%s\n%s" % (note, expanded)],
                                   "expanded": [expanded]},
                            "result": empty}
                src = expanded or 描述
                if two:
                    p = {}
                    for grp in (("①", "②", "③"), ("④", "⑤")):
                        raw = srv.complete(build_prompt(src, only=list(grp),
                                                        rich=True))
                        p.update(parse_out(raw))
                else:
                    p = parse_out(srv.complete(build_prompt(src)))
        except Exception as e:
            print("[AnimaPromptWriter] 失败: %s" % e)
            return {"ui": {"text": [""] * 6, "cn": ["生成失败：%s" % e]},
                    "result": empty}
        if not p:
            return {"ui": {"text": [""] * 6, "cn": ["模型没有输出可解析的五块"]},
                    "result": empty}
        # 拿**实际喂给模型的源文本**比对（拓展过就用拓展后的）
        p, _ = backfill(p, src)
        # 清垃圾/冲突标签：句子式、否定式同现、跨块姿态互斥
        p = resolve_contradiction(p)
        # 情绪互斥：原文写了情绪、模型却给了反向情绪（calm vs 滋火），代码兜住。
        # ⚠️ 必须**同时比用户原始描述**，不能只看 src ——
        #    实测「心里好一顿滋火却又无能为力」被**拓展阶段洗掉了**，
        #    src 里没有"滋火"，情绪检查就没触发，输出成了 `slightly anxious`。
        #    拓展可以润色，但不该能改掉用户的情绪意图。
        emo_src = "%s %s" % (描述 or "", src or "")
        ch = p.get("角色")
        if ch and ch["tags"]:
            ch["tags"], lost, gain = resolve_emotion(ch["tags"], emo_src)
            if lost or gain:
                print("[AnimaPromptWriter] 情绪互斥: 删 %s / 补 %s"
                      % (", ".join(lost) or "无", ", ".join(gain) or "无"))
        return pack(ground(p, src))


NODE_CLASS_MAPPINGS = {"AnimaPromptWriter": AnimaPromptWriter}
NODE_DISPLAY_NAME_MAPPINGS = {"AnimaPromptWriter": "Anima 提示词生成 (中文→五块)"}
