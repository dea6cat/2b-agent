"""Tests for the P1 tool-call robustness layer.

Covers the host-side arg coercion (tools.coerce_tool_args), the empty-name
guard in the dispatcher, and the conservative sampling / prompt-hint wiring.
Pure host-side logic — no model needed.
Run: `python -m unittest tests.test_toolcall_repair`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b import orchestrator, tools  # noqa: E402
from two_b.providers import ollama  # noqa: E402
from two_b.session import Session, Task  # noqa: E402

KNOWN = ("read_file", "edit_file", "write_file", "search_files", "list_files", "run_git")


class CoerceToolArgs(unittest.TestCase):
    def test_clean_call_is_unchanged(self):
        self.assertEqual(tools.coerce_tool_args("read_file", {"path": "a.py"}, KNOWN),
                         ("read_file", {"path": "a.py"}))

    def test_stringified_json_args_are_parsed(self):
        self.assertEqual(tools.coerce_tool_args("read_file", '{"path": "a.py"}', KNOWN),
                         ("read_file", {"path": "a.py"}))

    def test_nested_args_key_is_unwrapped(self):
        name, args = tools.coerce_tool_args("read_file", {"arguments": {"path": "a.py"}}, KNOWN)
        self.assertEqual((name, args), ("read_file", {"path": "a.py"}))

    def test_nested_args_alias_is_unwrapped(self):
        # 'args'/'input'/'parameters' are all accepted wrapper keys.
        for k in ("args", "input", "parameters"):
            _, args = tools.coerce_tool_args("read_file", {k: {"path": "a.py"}}, KNOWN)
            self.assertEqual(args, {"path": "a.py"}, k)

    def test_nested_stringified_args_are_parsed(self):
        _, args = tools.coerce_tool_args("read_file", {"arguments": '{"path": "a.py"}'}, KNOWN)
        self.assertEqual(args, {"path": "a.py"})

    def test_name_inside_wrapper_overrides_empty_outer_name(self):
        # The whole call arrived wrapped: {"name": "read_file", "arguments": {...}}.
        name, args = tools.coerce_tool_args("", {"name": "read_file", "arguments": {"path": "a.py"}}, KNOWN)
        self.assertEqual((name, args), ("read_file", {"path": "a.py"}))

    def test_tool_and_action_aliases_name_the_call(self):
        for k in ("tool", "action"):
            name, args = tools.coerce_tool_args("", {k: "list_files", "arguments": {"path": "."}}, KNOWN)
            self.assertEqual((name, args), ("list_files", {"path": "."}), k)

    def test_wrapper_name_overrides_unknown_outer_name(self):
        name, _ = tools.coerce_tool_args("call_1", {"name": "edit_file",
                                                    "arguments": {"path": "a", "old_text": "x", "new_text": "y"}}, KNOWN)
        self.assertEqual(name, "edit_file")

    def test_known_outer_name_is_not_overridden(self):
        # A valid outer name wins even if a stray 'name' field sits alongside real args.
        name, _ = tools.coerce_tool_args("write_file", {"arguments": {"path": "a", "content": "x"}}, KNOWN)
        self.assertEqual(name, "write_file")

    def test_real_call_with_extra_key_is_not_unwrapped(self):
        # search_files legitimately has query+path; an 'input'-like field alongside real
        # args means it is NOT a pure wrapper, so we leave it for the required-arg check.
        name, args = tools.coerce_tool_args("search_files", {"query": "x", "input": {"path": "."}}, KNOWN)
        self.assertEqual(name, "search_files")
        self.assertEqual(args, {"query": "x", "input": {"path": "."}})

    def test_garbage_args_become_empty_dict(self):
        # Not a dict, not JSON — yields {} so the required-arg check reports it cleanly.
        self.assertEqual(tools.coerce_tool_args("read_file", "not json", KNOWN), ("read_file", {}))
        self.assertEqual(tools.coerce_tool_args("read_file", 42, KNOWN), ("read_file", {}))
        self.assertEqual(tools.coerce_tool_args("read_file", None, KNOWN), ("read_file", {}))

    def test_name_is_stripped(self):
        self.assertEqual(tools.coerce_tool_args("  read_file  ", {"path": "a"}, KNOWN)[0], "read_file")

    def test_empty_wrapper_is_left_alone(self):
        # {"arguments": {}} is not a recoverable wrapper (no inner args); leave as-is.
        name, args = tools.coerce_tool_args("read_file", {"arguments": {}}, KNOWN)
        self.assertEqual(name, "read_file")

    def test_run_git_args_param_is_never_unwrapped(self):
        # run_git's own parameter is literally named 'args'; a JSON-object-shaped value
        # must NOT be unwrapped into different git arguments.
        name, args = tools.coerce_tool_args("run_git", {"args": '{"status": 1}'}, KNOWN + ("run_git",))
        self.assertEqual(name, "run_git")
        self.assertEqual(args, {"args": '{"status": 1}'})

    def test_mcp_tool_with_input_object_is_not_unwrapped(self):
        # A dynamically-schema'd MCP tool may legitimately take a sole 'input' object.
        known = ("srv__do_thing",)
        name, args = tools.coerce_tool_args("srv__do_thing", {"input": {"query": "hi", "n": 5}}, known)
        self.assertEqual(name, "srv__do_thing")
        self.assertEqual(args, {"input": {"query": "hi", "n": 5}})

    def test_wrapped_call_naming_a_non_file_tool_is_not_unwrapped(self):
        # Even fully wrapped, a non-file tool name is left for the empty-name/required
        # check rather than risk sending run_git the wrong shape.
        name, args = tools.coerce_tool_args("", {"name": "run_git", "arguments": {"args": "status"}},
                                            KNOWN + ("run_git",))
        self.assertEqual(name, "")


class EmptyNameGuard(unittest.TestCase):
    def setUp(self):
        self.session = Session(default_model="x")
        self.task = Task(description="t")

    def test_empty_name_returns_recoverable_error_not_crash(self):
        out = orchestrator._dispatch_tool(self.session, self.task, "", {})
        self.assertTrue(out.startswith("error: that tool call had no tool name"))
        self.assertIn("data, not a tool", out)

    def test_named_but_unknown_tool_still_reaches_unknown_branch(self):
        # A non-empty name is not caught by the empty-name guard.
        out = orchestrator._dispatch_tool(self.session, self.task, "frobnicate", {})
        self.assertEqual(out, "error: unknown tool frobnicate")


class SamplingAndPrompt(unittest.TestCase):
    def test_local_options_are_conservative_with_no_seed(self):
        p = ollama.OllamaProvider(name="ollama")
        # context_window would hit the network; stub it so _options stays pure.
        p.context_window = lambda model: 8192
        opts = p._options("qwen3.5:9b")
        self.assertEqual(opts["temperature"], 0.2)
        self.assertEqual(opts["repeat_penalty"], 1.1)
        self.assertEqual(opts["num_ctx"], 8192)
        # No pinned seed — an identical seed would make a repair retry repeat verbatim.
        self.assertNotIn("seed", opts)

    def test_cloud_options_omit_num_ctx_but_keep_sampling(self):
        p = ollama.OllamaProvider(name="ollama-cloud", api_key="k")
        opts = p._options("some-cloud-model")
        self.assertEqual(opts["temperature"], 0.2)
        self.assertNotIn("num_ctx", opts)

    def test_system_prompt_states_flat_arg_shapes(self):
        self.assertIn("do not nest them under an", orchestrator.SYSTEM_PROMPT)
        self.assertIn("edit_file{path, old_text, new_text}", orchestrator.SYSTEM_PROMPT)


class ParseArgsGuard(unittest.TestCase):
    """The native Ollama adapter must not raise on a malformed args string — it
    turns it into {} so coercion + the required-arg check can recover it."""

    def test_valid_json_string_is_parsed(self):
        self.assertEqual(ollama._parse_args('{"path": "a.py"}'), {"path": "a.py"})

    def test_malformed_json_string_becomes_empty_dict(self):
        # trailing comma / broken JSON — the top small-model malformation; must not raise.
        self.assertEqual(ollama._parse_args('{"path": "a.py",}'), {})
        self.assertEqual(ollama._parse_args("not json at all"), {})

    def test_non_object_json_becomes_empty_dict(self):
        self.assertEqual(ollama._parse_args("[1, 2, 3]"), {})

    def test_dict_passes_through(self):
        self.assertEqual(ollama._parse_args({"path": "a"}), {"path": "a"})


class MissingPathRecovery(unittest.TestCase):
    """When the model omits `path` on write_file/edit_file, the dispatcher
    infers one from context (title, last-read file, description, default)
    instead of dead-ending in a retry loop."""

    def setUp(self):
        self.session = Session(default_model="x", cwd="/tmp")
        self.task = Task(description="create a manual")

    def test_infers_from_content_title(self):
        content = "# My Flutter Guide\n\nSome content"
        path = orchestrator._infer_write_path(self.task, self.session, {"content": content})
        self.assertEqual(path, "my-flutter-guide.md")

    def test_infers_from_last_read(self):
        self.task.last_read_arg = "src/lib/main.dart"
        path = orchestrator._infer_write_path(self.task, self.session, {"content": ""})
        self.assertEqual(path, "src/lib/main.dart")

    def test_infers_from_description(self):
        path = orchestrator._infer_write_path(self.task, self.session, {"content": ""})
        self.assertEqual(path, "create-a-manual.md")

    def test_falls_back_to_timestamp_default(self):
        empty_task = Task(description="")
        path = orchestrator._infer_write_path(empty_task, self.session, {"content": ""})
        self.assertTrue(path.startswith("2b-output-"))
        self.assertTrue(path.endswith(".md"))

    def test_missing_path_is_inferred_not_errored(self):
        # The missing-path recovery kicks in for write_file, so the dispatch
        # should NOT return the old "missing required argument" error.
        # Use accept-edits mode so apply_write doesn't prompt.
        session = Session(default_model="x", cwd="/tmp", auto_yes=True)
        content = "# Test File\n\nHello"
        result = orchestrator._dispatch_tool(session, self.task, "write_file",
                                             {"content": content})
        self.assertNotIn("missing required argument", result)
        # The inferred path should be based on the title
        self.assertIn("test-file.md", result.lower() + result)


class CtxEnvKey(unittest.TestCase):
    """_ctx_env_key normalizes model names for TWOB_MODEL_CONTEXT_* env lookup."""

    def test_alphanumeric_preserved(self):
        self.assertEqual(ollama._ctx_env_key("gemma3"), "GEMMA3")

    def test_colons_and_dashes_become_underscores(self):
        self.assertEqual(
            ollama._ctx_env_key("gemma4:12b-mlx-16k"),
            "GEMMA4_12B_MLX_16K",
        )


class PerModelContextOverride(unittest.TestCase):
    """TWOB_MODEL_CONTEXT_<normalized_model_name> overrides _compute_ctx without
    a network call or RAM probe — lets the user pin e.g. 64k for gemma4:12b-mlx-16k
    when VS Code/Ollama recommends more than the model's reported context_length."""

    def setUp(self):
        self._saved = {}
        for var in list(os.environ):
            if var.startswith("TWOB_MODEL_CONTEXT_") or var == "TWOB_CONTEXT_TOKENS":
                self._saved[var] = os.environ.pop(var)

    def tearDown(self):
        os.environ.update(self._saved)
        for var in list(os.environ):
            if var.startswith("TWOB_MODEL_CONTEXT_") and var not in self._saved:
                del os.environ[var]

    def test_per_model_env_var_overrides_compute(self):
        os.environ["TWOB_MODEL_CONTEXT_GEMMA4_12B_MLX_16K"] = "65536"
        p = ollama.OllamaProvider(name="ollama")
        # _compute_ctx would hit the network; never called because env wins.
        self.assertEqual(p.context_window("gemma4:12b-mlx-16k"), 65536)
        # Cached — _compute_ctx is never invoked.
        self.assertEqual(p._ctx_cache["gemma4:12b-mlx-16k"], 65536)

    def test_global_env_var_still_wins_over_per_model(self):
        os.environ["TWOB_CONTEXT_TOKENS"] = "32768"
        os.environ["TWOB_MODEL_CONTEXT_GEMMA4_12B_MLX_16K"] = "65536"
        p = ollama.OllamaProvider(name="ollama")
        # Global override takes precedence.
        self.assertEqual(p.context_window("gemma4:12b-mlx-16k"), 32768)

    def test_no_env_falls_through_to_compute(self):
        p = ollama.OllamaProvider(name="ollama")
        # Stub _compute_ctx to avoid network; should be called when no env var set.
        p._compute_ctx = lambda model: 8192
        self.assertEqual(p.context_window("gemma4:12b-mlx-16k"), 8192)


if __name__ == "__main__":
    unittest.main()
