"""Fake Hermes agent loop for hermes-parity's own tests. Stdlib only.

Same surface the parity driver uses on a real tree: ``AIAgent(**kwargs).run_conversation()``,
an OpenAI-style chat.completions request to ``base_url``, the tool registry, and observer hooks
(pre/post_api_request, pre/post_tool_call) from enabled plugins in ``$HERMES_HOME/plugins``.
"""
import importlib.util
import json
import os
import urllib.request
from pathlib import Path

import hermes_time
import prompt
import tools.file_tools  # noqa: F401 - registers tools
from tools.registry import registry


def _enabled_plugins(home):
    cfg = home / "config.yaml"
    names, in_enabled = [], False
    for line in (cfg.read_text().splitlines() if cfg.exists() else []):
        if line.strip() == "enabled:":
            in_enabled = True
        elif in_enabled and line.strip().startswith("- "):
            names.append(line.strip()[2:])
        elif line and not line.startswith(" "):
            in_enabled = False
    return names


class _Ctx:
    def __init__(self, hooks):
        self.hooks = hooks

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)


class AIAgent:
    def __init__(self, model, api_key, base_url, enabled_toolsets=None, session_id="s", max_iterations=8, **_kw):
        self.model, self.base_url, self.session_id = model, base_url.rstrip("/"), session_id
        self.max_iterations = max_iterations
        self.hooks = {}
        home = Path(os.environ["HERMES_HOME"])
        for name in _enabled_plugins(home):
            init = home / "plugins" / name / "__init__.py"
            spec = importlib.util.spec_from_file_location(f"fake_plugin_{name.replace('-', '_')}", init)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.register(_Ctx(self.hooks))
        self.enabled = [n for n, e in registry._tools.items() if not enabled_toolsets or e.toolset in enabled_toolsets]
        self.system_prompt = prompt.build_system_prompt(hermes_time.now())

    def _hook(self, name, **kw):
        for fn in self.hooks.get(name, []):
            try:
                fn(session_id=self.session_id, turn_id="turn-1", telemetry_schema_version="hermes.observer.v1", **kw)
            except Exception:
                pass

    def run_conversation(self, user_message, conversation_history=None):
        messages = list(conversation_history or []) + [{"role": "user", "content": user_message}]
        self._hook("pre_llm_call", user_message=user_message)
        tools = [{"type": "function", "function": registry._tools[n].schema} for n in self.enabled]
        api_calls = 0
        while api_calls < self.max_iterations:
            body = {"model": self.model, "messages": [{"role": "system", "content": self.system_prompt}] + messages,
                    "tools": tools, "stream": False}
            self._hook("pre_api_request", message_count=len(body["messages"]), tool_count=len(tools),
                       api_call_count=api_calls + 1)
            req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            api_calls += 1
            choice = resp["choices"][0]
            msg = choice["message"]
            calls = msg.get("tool_calls") or []
            self._hook("post_api_request", finish_reason=choice["finish_reason"],
                       assistant_tool_call_count=len(calls), assistant_content_chars=len(msg.get("content") or ""),
                       assistant_message=msg)
            messages.append({k: v for k, v in msg.items() if v is not None})
            if not calls:
                return {"final_response": msg.get("content"), "messages": messages, "completed": True,
                        "api_calls": api_calls}
            for tc in calls:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"] or "{}")
                if name not in self.enabled:
                    result = json.dumps({"error": f"Unknown tool: {name}"})
                else:
                    self._hook("pre_tool_call", tool_name=name, args=args, tool_call_id=tc["id"])
                    result = registry._tools[name].handler(args)
                    self._hook("post_tool_call", tool_name=name, args=args, tool_call_id=tc["id"], result=result,
                               status="ok", error_type=None)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {"final_response": "", "messages": messages, "completed": False, "api_calls": api_calls}
