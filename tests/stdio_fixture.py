"""Small deterministic stdio fixture for integration tests."""

import json
import os
import sys
from pathlib import Path


def serve(mode: str, marker: Path) -> None:
    schema = {"type": "object"}
    if mode == "unsupported":
        schema = {
            "type": "object",
            "properties": {"n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
            "required": ["n"],
        }
    annotations = {"readOnlyHint": True}
    if mode == "mutation":
        annotations = {"readOnlyHint": False, "destructiveHint": True}
    if mode == "handoff":
        schema = {"type": "object", "properties": {"record_id": {"type": "integer"}}, "required": ["record_id"]}
    tool = {"name": "inspect_sample", "inputSchema": schema, "annotations": annotations}
    if mode == "missing-annotations":
        del tool["annotations"]
    if mode in ("invalid-output", "missing-output", "unverified-output"):
        tool["outputSchema"] = {
            "type": "object",
            "properties": {"count": {"$ref": "#/$defs/count"}},
            "required": ["count"],
            "$defs": {"count": {"type": "integer"}},
        }
        if mode == "unverified-output":
            tool["outputSchema"]["properties"]["record_id"] = {"$ref": "urn:chaoswalk:private-output-schema"}
    tools = [tool]
    if mode == "handoff":
        tools.append({"name": "get_handle", "inputSchema": {"type": "object"}, "annotations": annotations})

    catalog_change = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    calls = 0
    with marker.open("a", encoding="utf-8") as events:
        print("started", file=events, flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            method = request["method"]
            if method == "initialize":
                if mode == "stall-init":
                    continue
                response["result"] = {
                    "protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": mode.startswith("catalog-change")}},
                    "serverInfo": {"name": "test-fixture", "version": "1"},
                }
            elif method == "tools/list":
                if mode == "stall-list":
                    continue
                response["result"] = {"tools": tools}
                if mode == "pagination":
                    response["result"]["nextCursor"] = "another-page"
                if mode == "catalog-change-during-list":
                    print(json.dumps(catalog_change), flush=True)
            elif method == "tools/call":
                calls += 1
                print("called", file=events, flush=True)
                if mode == "catalog-change" and calls == 1:
                    tools = []
                    print(json.dumps(catalog_change), flush=True)
                if mode == "timeout":
                    continue
                if mode == "disconnect":
                    break
                if mode in ("invalid-params", "internal"):
                    code = -32602 if mode == "invalid-params" else -32603
                    response["error"] = {"code": code, "message": "private-error[/bold]"}
                elif mode == "handoff":
                    params = request["params"]
                    if params["name"] == "get_handle":
                        response["result"] = {"content": [], "structuredContent": {"record_id": 6429}}
                    elif params["arguments"].get("record_id") == 6429:
                        response["result"] = {"content": [], "structuredContent": {"status": "ready"}}
                    else:
                        response["error"] = {"code": -32602, "message": "Unexpected fixture record ID"}
                elif mode in ("invalid-output", "missing-output", "unverified-output"):
                    count = "private-invalid-count" if mode == "invalid-output" and calls == 1 else 3
                    structured = {"count": count}
                    if mode == "unverified-output" and calls == 1:
                        structured["record_id"] = 42
                    response["result"] = {"content": [], "structuredContent": structured}
                    if mode == "missing-output" and calls == 1:
                        response["result"] = {"content": [{"type": "text", "text": "private-text-only"}]}
                else:
                    response["result"] = {
                        "content": [],
                        "structuredContent": {
                            "order_id": 123,
                            "private_value": "private-response",
                            "had_credential": "CHAOSWALK_TEST_CREDENTIAL" in os.environ,
                        },
                    }
            print(json.dumps(response), flush=True)
        print("stopped", file=events, flush=True)


if __name__ == "__main__":
    serve(sys.argv[1], Path(sys.argv[2]))
