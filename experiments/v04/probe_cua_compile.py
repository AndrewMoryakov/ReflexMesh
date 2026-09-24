"""V0.4 pre-spike: would SystemOneHarness compile Cua driver MCP tools into actions? (no Cua install)

Tool input schemas are transcribed from the published cua-driver Linux MCP reference
(https://cua.ai/docs/reference/cua-driver/mcp-tools-linux, fetched 2026-09-24), reduced to the
parameters relevant for compilation. This is a documentation-based check; the real server's
schemas must be re-checked with `s1 tools --mcp "cua-driver mcp"` once Cua is installed.
"""
import json

from systemone_harness.tools import compile_tools

S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
CUA = [
    {"name": "observe", "description": "stand-in observe tool so compilation proceeds",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "get_window_state", "description": "AT-SPI tree + screenshot",
     "inputSchema": {"type": "object", "properties": {"pid": I, "window_id": I, "query": S,
                                                      "include_screenshot": B}, "required": ["pid"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "click", "description": "Click by element_index/element_token or x,y",
     "inputSchema": {"type": "object", "properties": {"pid": I, "window_id": I, "element_index": I,
                                                      "element_token": S, "snapshot_id": S, "x": N, "y": N,
                                                      "button": S, "count": I}, "required": []}},
    {"name": "type_text", "description": "Type text",
     "inputSchema": {"type": "object", "properties": {"pid": I, "text": S, "element_token": S},
                     "required": ["pid", "text"]}},
    {"name": "press_key", "description": "Press a key",
     "inputSchema": {"type": "object", "properties": {"pid": I, "key": S}, "required": ["pid", "key"]}},
    {"name": "scroll", "description": "Scroll",
     "inputSchema": {"type": "object", "properties": {"pid": I, "direction": {"type": "string",
                     "enum": ["up", "down", "left", "right"]}}, "required": ["direction"]}},
]

if __name__ == "__main__":
    try:
        cat = compile_tools(CUA)
        print(json.dumps({"compiled": sorted(cat.space.actions), "unsupported": cat.unsupported}, indent=1))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=1))
