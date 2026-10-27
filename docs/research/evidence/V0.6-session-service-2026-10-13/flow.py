import json, subprocess, sys, pathlib
S = pathlib.Path(sys.argv[1]); SOCK = str(S / "svc.sock")
def cli(cmd, payload=None):
    p = subprocess.run([str(pathlib.Path(sys.executable).parent / "reflexmesh"), "browser", cmd, "--socket", SOCK, *(["--input", "-"] if payload is not None else [])],
                       input=json.dumps(payload, ensure_ascii=False) if payload is not None else None, capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout or "{}")
def brief(tag, code, r):
    if r.get("schema_version") == "browser-progress/0.1":
        print(tag, "exit", code, "progress", r["observation"]["page"], "steps_left", r["assistance_steps_remaining"], "last", r.get("last_proposal"))
    else:
        print(tag, "exit", code, r.get("attempt_status"), r.get("stop_reason"), (r.get("handoff") or {}).get("reason"), "epoch", r.get("owner_epoch"), "chain", (r.get("chain_budget") or {}).get("used"), r.get("error"))
def cont(r, **kw):
    c = r["handoff"]["continuation"]; return {"token": c["token"], "parent_attempt_id": r["attempt_id"], **kw}
rev1 = json.loads((S / "rev1.json").read_text()); rev2 = json.loads((S / "rev2.json").read_text())
code, r = cli("submit", rev1); brief("A1 jev", code, r)
code, r = cli("resume", cont(r, operation="start", mode="jev", task=rev2)); brief("A2 jev", code, r)
n = 3
while r.get("attempt_status") == "incomplete" and (r.get("handoff") or {}).get("continuation", {}).get("allowed"):
    code, p = cli("resume", cont(r, operation="start", mode="llm")); brief(f"A{n} llm start", code, p); n += 1
    r = p
    # The caller (here: this script in the role of the LLM) picks one option by its visible name.
    for wanted in ("Save note",):
        if r.get("schema_version") != "browser-progress/0.1":
            break
        opt = next((o for o in r["action_options"] if o.get("name") == wanted), None)
        if opt is None:
            break
        code, r = cli("resume", {"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "step",
                                 "proposal": {"observation_id": r["observation"]["observation_id"], "operation": opt["operation"], "option_ref": opt["ref"]}})
        brief("   step " + wanted, code, r)
    if r.get("schema_version") == "browser-progress/0.1":
        code, r = cli("resume", {"token": r["continuation_token"], "parent_attempt_id": r["attempt_id"], "operation": "return_to_jev"}); brief("   return_to_jev", code, r)
    if n > 5: break
if "verification" in r:
    for v in r["verification"]: print("    ", v["id"], v["status"])
