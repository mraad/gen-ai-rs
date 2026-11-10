// Scripted LLM for the keyless demo. Its answers are canned, not generated.
const wantsMath = (text) => /multipl|\d\s*[x*×]\s*\d/i.test(text);
const wantsSkill = (text) => /convert/i.test(text);
const wantsPage = (text) => /on (the|this) page/i.test(text);

function toolCall(name, args) {
  return {
    message: {
      role: "assistant",
      content: null,
      tool_calls: [{ id: `mock_${name}`, type: "function", function: { name, arguments: JSON.stringify(args) } }],
    },
    finish_reason: "tool_calls",
  };
}

function lastUserText(messages) {
  return messages.filter((m) => m.role === "user").at(-1)?.content ?? "";
}

function reply(content) {
  return {
    message: { role: "assistant", content },
    finish_reason: "stop",
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

export async function mockChat({ messages }) {
  await new Promise((resolve) => setTimeout(resolve, 150));
  const last = messages.at(-1);
  const text = lastUserText(messages);
  if (last.role === "tool") {
    if (last.name === "activate_skill") {
      return toolCall("read_skill_resource", { name: "unit-conversion", path: "references/factors.md" });
    }
    if (last.name === "read_skill_resource") return reply(`[mock] From the skill's file: ${last.content.trim()}`);
    return reply(`[mock] The tool returned ${last.content}.`);
  }
  if (wantsPage(text)) return toolCall("page_read_messages", {});
  if (wantsSkill(text)) return toolCall("activate_skill", { name: "unit-conversion" });
  if (wantsMath(text)) {
    const [a = 6, b = 7] = (text.match(/\d+/g) ?? []).map(Number);
    return toolCall("multiply", { a, b });
  }
  return reply("[mock] Ask me to multiply numbers or convert miles to kilometers.");
}
