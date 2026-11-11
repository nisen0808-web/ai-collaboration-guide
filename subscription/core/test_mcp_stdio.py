import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from coordinator import Coordinator
from mcp_stdio import handle, serve


class MCPTests(unittest.TestCase):
    def test_in_process_tool_calls_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "ledger.db")
            self.assertIsNone(handle(core, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
            init = handle(core, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("Claude and Grok", init["result"]["instructions"])
            call = handle(core, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "collab_submit", "arguments": {"provider": "claude", "task": "hello", "request_key": "k"}}})
            self.assertEqual(call["result"]["structuredContent"]["status"], "not_submitted")

    def test_result_tool_retrieves_existing_job(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id): return {"status": "succeeded", "output": "done"}
        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "db", {"grok": Adapter()})
            core.submit("grok", "task", "key")
            response = handle(core, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "collab_result", "arguments": {"provider": "grok", "request_key": "key"}}})
            self.assertEqual(response["result"]["structuredContent"]["output"], "done")
            self.assertEqual(response["result"]["structuredContent"]["quality_verification"]["verified"], False)

    def test_actual_stdio_jsonlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "collab_status", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "collab_submit", "arguments": {"provider": "grok", "task": "中文测试🌐", "request_key": "unicode-1"}}},
            ]
            process = subprocess.run([sys.executable, str(Path(__file__).with_name("mcp_stdio.py")), "--db", str(Path(tmp) / "db")],
                                     input="\n".join(json.dumps(item, ensure_ascii=False) for item in messages) + "\n", text=True, encoding="utf-8", capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(process.stderr, "")
            result = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual({item["id"] for item in result}, {1, 2, 3, 4})
            status = next(item for item in result if item["id"] == 3)
            self.assertFalse(status["result"]["structuredContent"]["providers"]["grok"]["ready"])
            submission = next(item for item in result if item["id"] == 4)
            self.assertEqual(submission["result"]["structuredContent"]["status"], "not_submitted")

    def test_real_stdio_fake_adapter_returns_running_then_final_output(self):
        script = '''
import sys
from coordinator import Coordinator
from mcp_stdio import serve
class Adapter:
    def __init__(self): self.reads = 0
    def submit(self, **kwargs): return {"status":"queued", "job_id":"job-1"}
    def poll(self, job_id):
        self.reads += 1
        return {"status":"running"} if self.reads == 1 else {"status":"succeeded", "output":"done"}
serve(Coordinator(sys.argv[1], {"grok":Adapter()}), sys.stdin, sys.stdout)
'''
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.Popen([sys.executable, "-c", script, str(Path(tmp) / "db")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8")
            def call(number, name, arguments):
                child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}) + "\n")
                child.stdin.flush()
                result = json.loads(child.stdout.readline())
                self.assertEqual(result["id"], number)
                return result["result"]["structuredContent"]
            try:
                submitted = call(1, "collab_submit", {"provider": "grok", "task": "work", "request_key": "k"})
                self.assertEqual(submitted["status"], "queued")
                self.assertEqual(call(2, "collab_result", {"provider": "grok", "request_key": "k"})["status"], "running")
                final = call(3, "collab_result", {"provider": "grok", "request_key": "k"})
                self.assertEqual(final["status"], "succeeded")
                self.assertEqual(final["output"], "done")
            finally:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()
            self.assertEqual(child.returncode, 0)

    def test_hung_provider_does_not_block_status_or_other_provider(self):
        class InputQueue:
            def __init__(self):
                self.items = queue.Queue()
            def __iter__(self):
                while True:
                    line = self.items.get(timeout=5)
                    if line is None:
                        return
                    yield line
            def send(self, message):
                self.items.put(json.dumps(message) + "\n")

        class OutputQueue:
            def __init__(self):
                self.items = queue.Queue()
            def write(self, data):
                self.items.put(json.loads(data))
            def flush(self):
                pass

        entered = threading.Event()
        release = threading.Event()
        def hanging(**kw):
            entered.set()
            if not release.wait(4):
                raise TimeoutError("test synchronization")
            return {"status": "queued", "job_id": "claude-job"}
        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "db", {"claude": hanging,
                "grok": lambda **kw: {"status": "queued", "job_id": "grok-job"}})
            inp, out = InputQueue(), OutputQueue()
            server = threading.Thread(target=serve, args=(core, inp, out))
            server.start()
            try:
                inp.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "collab_submit", "arguments": {"provider": "claude", "task": "work", "request_key": "one"}}})
                self.assertTrue(entered.wait(3))
                inp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "collab_submit", "arguments": {"provider": "grok", "task": "work", "request_key": "two"}}})
                inp.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "collab_check", "arguments": {"request_key": "one"}}})
                responses = {item["id"]: item for item in (out.items.get(timeout=3), out.items.get(timeout=3))}
                self.assertEqual(responses[2]["result"]["structuredContent"]["job_id"], "grok-job")
                self.assertEqual(responses[3]["result"]["structuredContent"]["status"], "submission_uncertain")
                self.assertNotIn(1, responses)
            finally:
                release.set()
                inp.items.put(None)
                server.join(4)
            self.assertFalse(server.is_alive())

    def test_saturated_claude_does_not_consume_grok_or_control_slots(self):
        class InputQueue:
            def __init__(self): self.items = queue.Queue()
            def __iter__(self):
                while True:
                    item = self.items.get(timeout=5)
                    if item is None: return
                    yield json.dumps(item) + "\n"

        class OutputQueue:
            def __init__(self): self.items = queue.Queue()
            def write(self, data): self.items.put(json.loads(data))
            def flush(self): pass

        entered = threading.Event()
        release = threading.Event()
        gate = threading.Lock()
        invocations = []
        def hanging(**kw):
            with gate:
                invocations.append(kw)
                if len(invocations) == 2:
                    entered.set()
            if not release.wait(4):
                raise TimeoutError("test synchronization")
            return {"status": "queued", "job_id": kw["task_id"]}

        def call(number, name, arguments):
            return {"jsonrpc": "2.0", "id": number, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}

        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "db", {"claude": hanging,
                "grok": lambda **kw: {"status": "queued", "job_id": "grok-job"}})
            inp, out = InputQueue(), OutputQueue()
            server = threading.Thread(target=serve, args=(core, inp, out))
            server.start()
            try:
                for number in (1, 2):
                    inp.items.put(call(number, "collab_submit", {"provider": "claude", "task": "work", "request_key": str(number)}))
                self.assertTrue(entered.wait(3))
                inp.items.put(call(3, "collab_submit", {"provider": "claude", "task": "work", "request_key": "third"}))
                inp.items.put(call(4, "collab_submit", {"provider": "grok", "task": "work", "request_key": "grok"}))
                inp.items.put(call(5, "collab_status", {}))
                inp.items.put(call(6, "collab_check", {"request_key": "1"}))
                responses = {item["id"]: item for item in (out.items.get(timeout=3) for _ in range(4))}
                self.assertEqual(set(responses), {3, 4, 5, 6})
                self.assertEqual(responses[3]["error"]["code"], -32000)
                self.assertEqual(responses[4]["result"]["structuredContent"]["job_id"], "grok-job")
                self.assertIn("providers", responses[5]["result"]["structuredContent"])
                self.assertEqual(responses[6]["result"]["structuredContent"]["status"], "submission_uncertain")
                self.assertEqual(len(invocations), 2)
            finally:
                release.set()
                inp.items.put(None)
                server.join(4)
            self.assertFalse(server.is_alive())

    def test_saturated_result_poll_does_not_block_other_result_or_status(self):
        class InputQueue:
            def __init__(self): self.items = queue.Queue()
            def __iter__(self):
                while True:
                    item = self.items.get(timeout=5)
                    if item is None: return
                    yield json.dumps(item) + "\n"
        class OutputQueue:
            def __init__(self): self.items = queue.Queue()
            def write(self, data): self.items.put(json.loads(data))
            def flush(self): pass
        entered, release, gate = threading.Event(), threading.Event(), threading.Lock()
        calls = []
        class Claude:
            def submit(self, **kw): return {"status": "queued", "job_id": "claude-job"}
            def poll(self, job_id):
                with gate:
                    calls.append(job_id)
                    if len(calls) == 2: entered.set()
                if not release.wait(4): raise TimeoutError()
                return {"status": "running"}
        class Grok:
            def submit(self, **kw): return {"status": "queued", "job_id": "grok-job"}
            def poll(self, job_id): return {"status": "succeeded", "output": "done"}
        def call(number, name, arguments):
            return {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "db", {"claude": Claude(), "grok": Grok()})
            core.submit("claude", "task", "c")
            core.submit("grok", "task", "g")
            inp, out = InputQueue(), OutputQueue()
            server = threading.Thread(target=serve, args=(core, inp, out))
            server.start()
            try:
                for number in (1, 2):
                    inp.items.put(call(number, "collab_result", {"provider": "claude", "request_key": "c"}))
                self.assertTrue(entered.wait(3))
                inp.items.put(call(3, "collab_result", {"provider": "claude", "request_key": "c"}))
                inp.items.put(call(4, "collab_result", {"provider": "grok", "request_key": "g"}))
                inp.items.put(call(5, "collab_status", {}))
                responses = {item["id"]: item for item in (out.items.get(timeout=3) for _ in range(3))}
                self.assertEqual(set(responses), {3, 4, 5})
                self.assertEqual(responses[3]["error"]["code"], -32000)
                self.assertEqual(responses[4]["result"]["structuredContent"]["output"], "done")
                self.assertIn("providers", responses[5]["result"]["structuredContent"])
                self.assertEqual(len(calls), 2)
            finally:
                release.set()
                inp.items.put(None)
                server.join(4)
            self.assertFalse(server.is_alive())


if __name__ == "__main__":
    unittest.main()
