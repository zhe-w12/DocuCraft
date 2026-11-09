const TASK_VERB = String.raw`(?:写(?!作)|写作(?=$|[，。！？?、]|一篇|一份|一封|一段|文章|报告|文案|邮件|演讲稿|计划|方案|总结|通知|简历|作文|故事|脚本|内容|材料)|撰写|起草|生成|创作|编写|制作|构思|制定|制订|拟定|拟写|规划|策划|润色|修改|改写|重写|扩写|缩写|精简|总结|整理|提炼|翻译|续写|排版|校对|优化|完善|调整)`;
const TASK_ACTION = new RegExp(TASK_VERB);
const ACTION_AT_START = new RegExp(`^${TASK_VERB}`);
const DIRECT_TASK = new RegExp(`^(?:(?:请|帮我|帮忙|给我|替我|为我|我要|我想|我需要|直接)\\s*)+${TASK_VERB}`);
const CONTEXTUAL_TASK = new RegExp(`^(?:根据|按照|把|将).{0,32}${TASK_VERB}`);
const ASSISTED_TASK = new RegExp(`^(?:你能|可以|能不能).{0,10}(?:帮我|替我).{0,10}${TASK_VERB}`);
const EXPLICIT_ASSISTED_TASK = new RegExp(`(?:帮我|替我).{0,12}${TASK_VERB}`);
const CONVERSATIONAL_LEAD = /^(?:(?:请|麻烦)\s*)?(?:给我)?(?:讲讲|说说|介绍一下|解释一下|分析一下|告诉我|聊聊|谈谈|回答一下|简单介绍|简单说说)/;
const EXPLANATION_QUESTION = /(?:怎么|如何|为什么|什么是|是什么|能否|可不可以|怎么回事)|(?:吗|呢)[?？。！!]?$/;
const DOCUMENT_CONTINUATION = /^(?:继续|接着|沿用|基于(?:上文|这份|刚才)|按(?:这个|刚才|上面)|再(?:写|改|润|补|扩|缩|加|生成|整理|总结|来一版)|(?:把|将).{0,24}(?:改|调整|补|增加|删|润|扩|缩)|第[一二三四五六七八九十\d]+段)/;

/** Resolve an `auto` turn before rendering begins so conversational replies never open or overwrite the document. */
export function resolveAssistantWorkflow(mode, content, previousMessages = []) {
  if (mode !== "auto") return mode;

  const text = String(content || "").trim();
  if (!text) return "chat";

  const previousAssistant = [...previousMessages].reverse().find(message => message?.role === "assistant");
  const hasDocumentContext = previousAssistant?.workflow && previousAssistant.workflow !== "chat";

  if (
    ACTION_AT_START.test(text) || DIRECT_TASK.test(text) || CONTEXTUAL_TASK.test(text) ||
    ASSISTED_TASK.test(text) || EXPLICIT_ASSISTED_TASK.test(text)
  ) return "auto";
  if (CONVERSATIONAL_LEAD.test(text) || EXPLANATION_QUESTION.test(text)) return "chat";
  if (hasDocumentContext && (DOCUMENT_CONTINUATION.test(text) || TASK_ACTION.test(text))) return "auto";

  return "chat";
}
