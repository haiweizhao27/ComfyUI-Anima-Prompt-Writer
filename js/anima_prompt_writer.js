import { app } from "../../scripts/app.js";

/**
 * Anima Prompt Writer 前端
 *
 * 六个输出框是**节点输入**（在 nodes.py 的 INPUT_TYPES 里声明成 multiline STRING），
 * 所以 ComfyUI 原生把它们渲染成真正的多行文本框 —— 可编辑、可滚动、内容不被截断。
 * 用 JS 的 addWidget("text") 只能得到单行 widget，文字会被截断。
 *
 * 前端只做三件事：
 *   1. 后端执行完 -> 把结果写进六个框 + 中文注释框
 *   2. 描述变了 -> 清空六个框，强制重新生成（否则会拿旧的）
 *   3. 「清空修正并重新生成」按钮
 */
const BLOCKS = ["框1角色", "框2衣着", "框3动作", "框4环境", "框5画风"];
const WHOLE = "整体框";
const CN = "中文注释";
const EXPAND = "拓展框";
const ALL = BLOCKS.concat([WHOLE]);

// 这几个是后端根据输出方式自动决定的，前端不用管
const OUTPUT_ONLY = new Set([WHOLE]);

function w(node, name) {
    return (node.widgets || []).find((x) => x.name === name);
}

function isOverall(node) {
    const m = w(node, "输出方式");
    return m && String(m.value).indexOf("整体") >= 0;
}

function applyMode(node) {
    const overall = isOverall(node);
    for (const name of BLOCKS) {
        const x = w(node, name);
        if (!x) continue;
        if (overall) {
            if (!x.__orig) x.__orig = x.computeSize;
            x.computeSize = () => [0, -4];
        } else if (x.__orig) {
            x.computeSize = x.__orig;
            x.__orig = null;
        }
        x.hidden = overall;
    }
    const ow = w(node, WHOLE);
    if (ow) ow.hidden = !overall;
    // **不要在这里 setSize** —— 那会覆盖用户手动拉的大小，
    // 表现就是"出完提示词整个节点缩回去"。只重绘画布让隐藏生效即可。
    app.graph.setDirtyCanvas(true, true);
}

function clearBoxes(node) {
    for (const name of ALL) {
        const x = w(node, name);
        if (x) x.value = "";
    }
}

app.registerExtension({
    name: "Anima.PromptWriter",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "AnimaPromptWriter") return;
        const onCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated ? onCreated.apply(this, arguments) : undefined;
            const node = this;

            // 中文注释框：只读，别让它被改
            const cn = w(node, CN);
            if (cn && cn.inputEl) {
                cn.inputEl.readOnly = true;
                cn.inputEl.style.opacity = "0.75";
            }

            // 描述一变就清空输出框和拓展框 ——
            // 拓展框不清的话，下次会拿旧拓展（对应旧描述）去生成。
            const desc = w(node, "描述");
            if (desc) {
                const orig = desc.callback;
                desc.callback = function (v) {
                    if (orig) orig.apply(this, arguments);
                    clearBoxes(node);
                    const ex = w(node, EXPAND);
                    if (ex) ex.value = "";
                };
            }

            // 输出方式切换后重排
            const mode = w(node, "输出方式");
            if (mode) {
                const orig = mode.callback;
                mode.callback = function (v) {
                    if (orig) orig.apply(this, arguments);
                    applyMode(node);
                };
            }

            // 清空按钮：清掉六个框，强制重新生成
            node.addWidget("button", "↻ 清空修正并重新生成", null, () => {
                clearBoxes(node);
                try { app.queuePrompt(0, 1); }
                catch (e) { try { app.queuePrompt(); } catch (e2) { /* 忽略 */ } }
            });

            applyMode(node);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            if (onExecuted) onExecuted.apply(this, arguments);
            const node = this;
            const vals = (message && message.text) || [];
            BLOCKS.forEach((name, i) => {
                const x = w(node, name);
                if (x) x.value = vals[i] || "";
            });
            const ow = w(node, WHOLE);
            if (ow) ow.value = vals[5] || "";
            const cn = w(node, CN);
            if (cn) cn.value = (message && message.cn && message.cn[0]) || "";
            // 补充后的中文写进可编辑的「拓展框」—— 你可以直接改，
            // 下次执行会用你改过的版本，不再重新拓展。
            const ex = w(node, EXPAND);
            if (ex) {
                const e = (message && message.expanded && message.expanded[0]) || "";
                if (e) ex.value = e;
            }
            // **不要 setSize** —— 出完结果节点缩回去就是这个引起的。
            // 保持用户手动拉的尺寸，只刷新画布。
            app.graph.setDirtyCanvas(true, true);
        };
    },
});
