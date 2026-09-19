# ComfyUI Anima Prompt Writer

把**中文画面描述**转成**五块结构化英文提示词**的 ComfyUI 节点。完全本地运行，不联网。

```
中文描述  →  本地 Qwen3-4B  →  ①角色 ②衣着 ③动作 ④环境 ⑤画风
                              + 术语库检索（RAG）+ 接地检查
```

为 [Anima](https://huggingface.co/circlestone-labs/Anima) 这类被 Qwen3 文本编码器驱动的模型设计。
**标签一律用空格、不用下划线** —— 见下面「为什么不用下划线」。

## 特性

- **五块结构化输出**：①角色 ②衣着 ③动作 ④环境 ⑤画风，每块带英文标签 + 英文叙事 + 中文解释
- **术语库检索（RAG）**：33000+ 条标签 / 8000 个角色，命中后作为标准写法注入提示词，不靠模型背词
- **术语回填**：模型漏抄的库内术语，直接补进对应块（实测反复丢过 `cleavage`、`hanfu`、`keqing` 等）
- **接地检查**：输出后反查术语库，标出"源文本里找不到依据"的可疑标签。**只提示，不删**
- **确定性后处理**：下划线→空格、跨块去重、姿态/情绪互斥、句子式标签清理、年龄标签禁入
- **跑完即释放显存**：`terminate()` → 超时 `kill()`，并轮询等显存回到起之前的水位
- **中文解释区可编辑回流**：框里改过的内容下次直接采用，不再走模型

## 安装

### 1. 插件

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/haiweizhao27/ComfyUI-Anima-Prompt-Writer.git ComfyUI-Anima-Prompt-Writer
```

### 2. 模型（GGUF）

把任意 **Qwen3-4B-Instruct-2507** 的 GGUF 放进 `ComfyUI/models/LLM/`（节点会自动扫描该目录）：

| 来源 | 链接 |
|---|---|
| unsloth（推荐，量化齐全） | https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF |
| 官方基座 | https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507 |

推荐 `Q4_K_M` 量化，约 2.3 GB。其他量化（Q5/Q6/Q8）也能跑，越大越慢越准。

### 3. llama.cpp

从 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 下载对应平台的构建包，
解压后把目录设进环境变量 `LLAMA_CPP_DIR`：

```bash
# Windows (PowerShell，永久生效)
setx LLAMA_CPP_DIR "D:\path\to\llama.cpp"
# Linux / macOS
export LLAMA_CPP_DIR=/path/to/llama.cpp
```

也可以直接把解压出来的文件放进本插件的 `bin/` 目录。

> 需要的是带 `llama-server` 可执行文件的那一层目录。装好后重启 ComfyUI。

## 用法

节点在 `Anima/提示词` 分类下，名字 **Anima 提示词生成 (中文→五块)**。

| 输入 | 说明 |
|---|---|
| `描述` | 中文画面描述 |
| `模式` | `丰富`（默认，不限）／`简约`（每块 ≤4 标签） |
| `输出方式` | `块输出`（五块分开）／`整体输出`（合并成一条） |
| `分段` | `不分段`（默认）／`分段两轮`（①②③ + ④⑤ 两次调用）。实测标签总数不变而耗时翻倍，故默认单轮 |
| `接地检查` | `标记(不删)`（默认）／`关闭`。见下节 |
| `用拓展中文` | `是`＝先让模型把描述补完整，再**用补充后的中文**翻译成五块；补充结果写进 `拓展框`，可人工编辑 |
| `仅拓展中文` | `是`＝只出中文就收工，不出 tags。用于「先跑一遍 → 删掉模型编多的句子 → 再生成」 |
| `显存策略` | `自动`（先量空闲显存，低于 4 GB 退纯 CPU）／`全GPU`／`纯CPU` |
| `框1角色`…`框5画风`、`整体框` | **可留空**。填了就**直接采用**，不再跑模型 |
| `拓展框` | 可留空。填了就用你编辑过的中文，不再重新拓展 |
| `中文注释` | 输出（只读）：每块吃进了哪些中文 + 接地检查报告 |
| `启用` | 关掉就直通空值，不跑模型 |

**输出**：`①角色` `②衣着` `③动作` `④环境` `⑤画风` `整体` —— 六个框里只有英文。

## 为什么标签用空格而不是下划线

Anima 的文本编码器是 **Qwen3（语言模型）**，吃自然语言，不是 CLIP。

实测对比：`holding_a_holo_umbrella` 出图时**把画面里的狗整个丢了**，
换成 `holding a holo umbrella` 狗就出来了。

所以本插件的术语库命中、模型输出、后处理**全程统一成空格分隔**。
库里混着两种写法（`blue sky` 与 `blue_sky`），会在输出前统一。

## 接地检查（只提示，不删标签）

输出后逐个标签**反查术语库**，看它对应的中文在源文本里有没有依据，
结果写进「中文注释」。堵的是**翻译阶段的编造** —— 把「室内」翻成
`clean walls, white ceiling` 这种。

```
【接地检查】接地 6 / 推断 1 / 未知 1 / 未接地 2
  可疑（源文本里找不到依据，可能是编的，也可能是合理推断）：
    · sun(太阳)　← 整标签命中，最可疑
    · moonlight glow　（词根推断，仅供参考）
```

**它只提示，不会删任何标签。** 曾经做过一版「删除未接地」，实测否掉了：

| 标签 | 判定 | 该删吗 |
|---|---|---|
| `standing`（站立） | 未接地 | ✗「伸手接花瓣」当然蕴含站立 |
| `soft fabric`（柔软织物） | 未接地 | ✗ 汉服当然是织物 |
| `white ceiling`（天花板） | 未接地 | ✓ 真编造，却漏了 |

**它删掉的全是合理推断，真正该抓的反而漏了。** 根因是原理性的：
拿"源文本里有没有"来判定，**分不开「合理推断」和「编造」**。

判定为「未接地」但**术语库里查不到**的标签一律保留 ——
很多新造复合词（`golden mechanical dog`）库里没有，查不到就删是误杀。
检查范围只有 `①角色②衣着④环境`：③动作是合理推断、⑤画风是风格选择，
源文本里本来就不会写，硬查只会满屏误报。

## 显存

**出图要用卡，所以这个节点把显存当第一约束。**

1. 起模型前先量空闲显存，`自动` 模式下低于 4 GB 就退纯 CPU，完全不占卡
2. 跑完 `terminate()` → 超时 `kill()`，确保进程真的死掉
3. **轮询等显存回到起之前的水位**才返回

代价是每次执行都要重新加载模型，单次请求约 **10~20 秒**（取决于输入长度和量化）。

## 已知限制

- **跑批不稳定**：同一句输入两次跑，`汉服` 可能一次出 `hanfu`、一次降级成 `robe`
- **拓展阶段会编造**：原文「现代城市」可能被补成"高耸玻璃建筑、绿意公园"。
  想压掉就把「用拓展中文」设成 `否`，或用「仅拓展中文」先跑一遍手工删句子
- **库里没有的角色名会被静默丢掉**。不猜外观、不报警告
- **偶尔跨块串味**：①里出现本该归②的服装
- **抽象情绪容易译成"句子式标签"**（如 `deep sense of exhaustion and energy`），
  这类对出图几乎没有驱动作用。具体物件型输入的命中率明显高于抽象情绪型
- 输出偏**臃肿**：常有若干标签在重复表达同一件事

## 重建术语库（可选）

`glossary.json` 可以用 `build_glossary.py` 重新生成：

```bash
# 自动在同级 custom_nodes 下找 Comfyui-Anima-Tools
python build_glossary.py
# 或显式指定
python build_glossary.py --anima-tools /path/to/Comfyui-Anima-Tools-main
# 额外生成角色库（需要 character_data.js）
python build_glossary.py --chars
# 先输出到别处看看效果，不动现用的库
python build_glossary.py --out /tmp/gl.json
```

数据来源：

| 来源 | 必需 | 说明 |
|---|---|---|
| [Comfyui-Anima-Tools](https://github.com/luguoyin/Comfyui-Anima-Tools) `js/*_data.js` | ✅ | MIT。**13338 条**逐标签中英对照 + 1789 套装 |
| `themes_v21.json`（放本目录） | 可选 | 补充带块归属的标签。**不在本仓库内** |
| `character_data.js` + `sm_all.json` | 可选 | `--chars` 时生成 `chars.json` |

> **说明**：仓库里附带的 `glossary.json`（33098 条标签）**比只用 Anima-Tools
> 构建出来的（13338 条）要多** —— 多出的部分来自作者本地的一份
> `themes_v21.json`，该文件不在本仓库内。所以 `build_glossary.py`
> 单独跑会得到一个小一些、但**完全可复现**的库。想要完整版就自己补
> `themes_v21.json`，或者直接用仓库里这份。

## 角色库（可选）

仓库里的 `glossary.json` **只含标签库**（33000+ 条标签），不含角色数据库。

角色数据库（8000 个动漫角色，用于识别「刻晴」「初音未来」这类名字并自动补上
系列 / 发色 / 瞳色）**不随本仓库分发** —— 那部分数据的版权来源比标签库复杂，
不适合直接再分发。

想要角色功能，就在本插件目录下放一个 `chars.json`（结构：
`{"chars": [...], "char_index": {...}}`）。有它就用，没有就静默跳过、不影响其他功能。
该文件已在 `.gitignore` 里，不会被误提交。

## 致谢与许可

本项目的**术语库数据**（`glossary.json`）基于
[Comfyui-Anima-Tools](https://github.com/luguoyin/Comfyui-Anima-Tools)（**MIT License**）
构建 —— 其标签对照表是本插件 RAG 的数据基础。
按该项目的许可要求，特此保留版权声明与致谢。

模型与推理依赖：

- [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) — Apache-2.0
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — MIT

本项目基于 **MIT License** 开源，见 [LICENSE](LICENSE)。
