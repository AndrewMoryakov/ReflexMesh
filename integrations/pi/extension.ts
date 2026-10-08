/**
 * reflex-mesh-pi: registers the ReflexMesh Task API (MCP over stdio) for a Pi session.
 *
 * Pi stays a client (INV-01): this file only starts `python -m reflexmesh mcp`; the core does not
 * know about Pi. Configuration comes from the environment:
 *
 *   REFLEXMESH_MCP_COMMAND  JSON array that replaces the whole command, e.g. to run the server on
 *                           another host over ssh: ["ssh", "host", "python3 -m reflexmesh mcp ..."]
 *   REFLEXMESH_PYTHON       Python interpreter (default: python3)
 *   REFLEXMESH_PYTHONPATH   PYTHONPATH for the server (e.g. <checkout>/src:<SystemOneHarness>)
 *   REFLEXMESH_STATE_DIR    attempt directories (default: ~/.local/state/reflexmesh)
 *   REFLEXMESH_CHROME       Chromium executable for browser attempts
 *   REFLEXMESH_JEV_URL      loopback JevRouter for --routing-provider jevrouter
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function command(): { command: string; args: string[] } {
  const override = process.env.REFLEXMESH_MCP_COMMAND;
  if (override) {
    const parsed: unknown = JSON.parse(override);
    if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every((part) => typeof part === "string")) {
      throw new Error("REFLEXMESH_MCP_COMMAND must be a nonempty JSON array of strings");
    }
    return { command: parsed[0], args: parsed.slice(1) };
  }
  const args = ["-m", "reflexmesh", "mcp", "--state-dir",
                process.env.REFLEXMESH_STATE_DIR ?? "~/.local/state/reflexmesh"];
  if (process.env.REFLEXMESH_CHROME) args.push("--chrome", process.env.REFLEXMESH_CHROME);
  if (process.env.REFLEXMESH_JEV_URL) args.push("--jev-url", process.env.REFLEXMESH_JEV_URL);
  return { command: process.env.REFLEXMESH_PYTHON ?? "python3", args };
}

export default function (pi: ExtensionAPI) {
  const { command: executable, args } = command();
  const env: Record<string, string> = {};
  if (process.env.REFLEXMESH_PYTHONPATH) env.PYTHONPATH = process.env.REFLEXMESH_PYTHONPATH;
  pi.registerMcpServer("reflexmesh", {
    command: executable,
    args,
    env,
    // Four small tools used in a fixed loop: declare them to the model directly.
    exposure: "direct",
    description: "ReflexMesh: run one bounded, verified browser subtask; returns a result or a handoff",
    // A status call waits at most 50 s; leave room for the transport.
    timeout: 90,
  });
}
