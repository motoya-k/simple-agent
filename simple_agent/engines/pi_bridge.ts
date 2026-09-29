// pi extension: expose simple-agent's own tools (memory, skills, session
// search) to pi. pi has no MCP client, so this starts `simple-agent --mcp`,
// lists its tools, and registers each one with pi.registerTool.
//
// The PiEngine loads it with `-e` and passes the server command as a JSON argv
// array in SIMPLE_AGENT_MCP_COMMAND. The allowlist is already in that command,
// so pi can only see the tools the route allowed.

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

type Reply = { result?: any; error?: { message: string } };

export default async function (pi: ExtensionAPI) {
  const argv: string[] = JSON.parse(process.env.SIMPLE_AGENT_MCP_COMMAND ?? "[]");
  if (argv.length === 0) return;

  const child = spawn(argv[0], argv.slice(1), { stdio: ["pipe", "pipe", "inherit"] });
  const waiting = new Map<number, (reply: Reply) => void>();
  let nextId = 1;

  // The server must not keep pi alive: in -p mode pi exits when its event loop
  // drains, and an open child pipe would hold it forever. So the child and its
  // pipes count toward the loop only while a request is in flight.
  const pipes = [child, child.stdin as any, child.stdout as any];
  const hold = (on: boolean) => pipes.forEach((p) => (on ? p.ref?.() : p.unref?.()));
  hold(false);

  createInterface({ input: child.stdout! }).on("line", (line) => {
    let reply: Reply & { id?: number };
    try {
      reply = JSON.parse(line);
    } catch {
      return;
    }
    const done = reply.id === undefined ? undefined : waiting.get(reply.id);
    if (done) {
      waiting.delete(reply.id!);
      if (waiting.size === 0) hold(false);
      done(reply);
    }
  });
  process.on("exit", () => child.kill());

  const call = (method: string, params: object = {}) =>
    new Promise<any>((resolve, reject) => {
      const id = nextId++;
      hold(true);
      waiting.set(id, (reply) => (reply.error ? reject(new Error(reply.error.message)) : resolve(reply.result)));
      child.stdin!.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });

  await call("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "pi" } });
  child.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  const { tools } = await call("tools/list");

  for (const tool of tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: Type.Unsafe<Record<string, unknown>>(tool.inputSchema),
      async execute(_toolCallId, params) {
        const result = await call("tools/call", { name: tool.name, arguments: params });
        const text = (result.content ?? []).map((c: any) => c.text ?? "").join("");
        if (result.isError) throw new Error(text);
        return { content: [{ type: "text", text }], details: {} };
      },
    });
  }
}
