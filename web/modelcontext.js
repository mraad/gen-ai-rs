// shortcut: same-document WebMCP (document.modelContext) for browsers without it: registerTool, getTools,
// executeTool only; no events, frames, exposedTo, fromOrigins or annotations. Drop it once WebMCP ships.
if (!("modelContext" in document)) {
  const tools = new Map();
  const fail = (name, message) => new DOMException(message, name);
  const context = {
    shim: true,
    async registerTool(tool, { signal } = {}) {
      if (!tool?.name || !tool.description || typeof tool.execute !== "function") throw new TypeError("tool needs name, description and execute");
      if (tools.has(tool.name)) throw fail("InvalidStateError", `tool '${tool.name}' is already registered`);
      if (signal?.aborted) return;
      const entry = { ...tool, inputSchema: tool.inputSchema ? JSON.stringify(tool.inputSchema) : "" };
      tools.set(tool.name, entry);
      signal?.addEventListener("abort", () => { if (tools.get(tool.name) === entry) tools.delete(tool.name); }, { once: true });
    },
    async getTools() {
      return [...tools.values()].map(({ name, description, inputSchema }) => ({ name, description, inputSchema }));
    },
    async executeTool(tool, input = {}, { signal = new AbortController().signal } = {}) {
      const entry = tools.get(tool?.name);
      if (!entry) throw fail("NotFoundError", `tool '${tool?.name}' is not registered`);
      signal.throwIfAborted();
      try {
        const result = await Promise.race([
          entry.execute(JSON.parse(JSON.stringify(input)), { signal }),
          new Promise((_, reject) => signal.addEventListener("abort", () => reject(fail("AbortError", "tool call cancelled")), { once: true })),
        ]);
        return JSON.stringify(result ?? null);
      } catch (error) {
        throw error instanceof DOMException && error.name === "AbortError" ? error : fail("OperationError", error?.message ?? String(error));
      }
    },
  };
  Object.defineProperty(document, "modelContext", { value: context, configurable: true });
}
