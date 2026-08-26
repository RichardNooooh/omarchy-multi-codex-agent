import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateWrapperTests(unittest.TestCase):
    def install_flock_shim(self, bin_dir):
        real_flock = shutil.which("flock")
        self.assertIsNotNone(real_flock)
        shim = bin_dir / "flock"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$$\" >> \"$FLOCK_TRACE\"\n"
            "exec \"$REAL_FLOCK\" \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return real_flock

    def wait_for(self, predicate):
        for _ in range(100):
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for test signal")

    def test_codex_record_is_staged_until_enrichment_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            processor = directory / "codex_accounts.py"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            processor.write_text(
                "import json, os, sys\n"
                "with open(os.environ['PROCESSOR_STARTED'], 'w') as handle: handle.write('started')\n"
                "with open(os.environ['STAGE_DEVICE'], 'w') as handle: handle.write(str(os.stat(os.path.dirname(sys.argv[2])).st_dev))\n"
                "while not os.path.exists(os.environ['PROCESSOR_RELEASE']): pass\n"
                "with open(sys.argv[2], encoding='utf-8') as handle: record = json.load(handle)\n"
                "record['accounts'] = [{'label': 'Enriched'}]\n"
                "with open(sys.argv[2], 'w', encoding='utf-8') as handle: json.dump(record, handle)\n",
                encoding="utf-8",
            )
            omarchy_bin = directory / "omarchy/bin"
            omarchy_bin.mkdir(parents=True)
            collector = omarchy_bin / "omarchy-agent-usage-codex"
            collector.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            collector.chmod(0o755)
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "usage=\"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "mkdir -p \"$usage\"\n"
                "printf '%s' '{\"id\":\"codex\",\"stock\":true}' > \"$usage/codex.json\"\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            state_home = directory / "state"
            real_record = state_home / "omarchy/agents/usage/codex.json"
            unrelated_record = state_home / "omarchy/agents/usage/claude.json"
            real_record.parent.mkdir(parents=True)
            real_record.write_text('{"id":"codex","previous":true}', encoding="utf-8")
            unrelated_record.write_text('{"id":"claude","previous":true}', encoding="utf-8")
            started = directory / "processor-started"
            stage_device = directory / "stage-device"
            release = directory / "processor-release"
            environment = os.environ | {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "XDG_STATE_HOME": str(state_home),
                "OMARCHY_PATH": str(directory / "omarchy"),
                "PROCESSOR_STARTED": str(started),
                "STAGE_DEVICE": str(stage_device),
                "PROCESSOR_RELEASE": str(release),
            }
            process = subprocess.Popen([str(wrapper), "--limits-only"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            for _ in range(100):
                if started.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(started.exists())
            self.assertEqual(stage_device.read_text(encoding="utf-8"), str(real_record.parent.stat().st_dev))
            self.assertEqual(json.loads(real_record.read_text(encoding="utf-8")), {"id": "codex", "previous": True})
            release.touch()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
            self.assertEqual(json.loads(real_record.read_text(encoding="utf-8")), {"id": "codex", "stock": True, "accounts": [{"label": "Enriched"}]})
            self.assertEqual(json.loads(unrelated_record.read_text(encoding="utf-8")), {"id": "claude", "previous": True})
            self.assertEqual([path for path in real_record.parent.glob(".agent-usage-update.*") if path.is_dir()], [])

    def test_publication_failure_is_generic_and_preserves_the_existing_record(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            (bin_dir / "omarchy-agent-usage-update").write_text(
                "#!/usr/bin/env bash\nmkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\nprintf '%s' '{\"id\":\"codex\",\"stock\":true}' > \"$XDG_STATE_HOME/omarchy/agents/usage/codex.json\"\n",
                encoding="utf-8",
            )
            (bin_dir / "mv").write_text("#!/usr/bin/env bash\nprintf 'publication path /secret\\n' >&2\nexit 12\n", encoding="utf-8")
            for executable in (bin_dir / "omarchy-agent-usage-update", bin_dir / "mv"):
                executable.chmod(0o755)
            state_home = directory / "state"
            record = state_home / "omarchy/agents/usage/codex.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"id":"codex","previous":true}', encoding="utf-8")
            result = subprocess.run(
                [str(wrapper), "codex"], text=True, capture_output=True,
                env=os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(state_home)},
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr.splitlines(), ["agent-usage-update: record publication failed"])
            self.assertNotIn("secret", result.stderr)
            self.assertEqual(json.loads(record.read_text(encoding="utf-8")), {"id": "codex", "previous": True})

    def test_unset_omarchy_path_does_not_publish_default_records(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text("#!/usr/bin/env bash\nmkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\nprintf '%s' '{\"id\":\"codex\"}' > \"$XDG_STATE_HOME/omarchy/agents/usage/codex.json\"\n", encoding="utf-8")
            stock.chmod(0o755)
            environment = os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(directory / "state")}
            environment.pop("OMARCHY_PATH", None)
            result = subprocess.run([str(wrapper)], text=True, capture_output=True, env=environment)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                (directory / "state/omarchy/agents/usage/codex.json").exists(),
                os.access("/bin/omarchy-agent-usage-codex", os.X_OK),
            )

    def test_partial_failure_leaves_unchanged_seeded_record_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "mkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "printf '%s' '{\"id\":\"claude\",\"updated\":true}' > \"$XDG_STATE_HOME/omarchy/agents/usage/claude.json\"\n"
                "exit 7\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            usage = directory / "state/omarchy/agents/usage"
            usage.mkdir(parents=True)
            unchanged = usage / "fireworks.json"
            unchanged.write_text('{"id":"fireworks","previous":true}', encoding="utf-8")
            before = unchanged.stat()
            result = subprocess.run([str(wrapper), "claude", "fireworks"], text=True, capture_output=True, env=os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(directory / "state")})
            after = unchanged.stat()
            self.assertEqual(result.returncode, 7)
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            self.assertEqual(json.loads((usage / "claude.json").read_text(encoding="utf-8")), {"id": "claude", "updated": True})

    def test_serializes_refreshes_so_later_results_win(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text(
                "import json, os, sys\n"
                "with open(sys.argv[2], encoding='utf-8') as handle: record = json.load(handle)\n"
                "with open(os.environ['PROCESSOR_TRACE'], 'a') as handle: handle.write(record['version'] + '\\n')\n"
                "if record['version'] == 'old':\n"
                "    while not os.path.exists(os.environ['PROCESSOR_RELEASE']): pass\n",
                encoding="utf-8",
            )
            (bin_dir / "omarchy-agent-usage-update").write_text(
                "#!/usr/bin/env bash\n"
                "mkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "version=old; [[ $* == *--force* ]] && version=new\n"
                "[[ $version == new ]] && : > \"$SECOND_STOCK_STARTED\"\n"
                "printf '{\"id\":\"codex\",\"version\":\"%s\"}' \"$version\" > \"$XDG_STATE_HOME/omarchy/agents/usage/codex.json\"\n",
                encoding="utf-8",
            )
            (bin_dir / "omarchy-agent-usage-update").chmod(0o755)
            real_flock = self.install_flock_shim(bin_dir)
            flock_trace = directory / "flock-trace"
            trace = directory / "processor-trace"
            release = directory / "processor-release"
            second_process_started = directory / "second-process-started"
            second_stock_started = directory / "second-stock-started"
            second = directory / "second-refresh"
            second.write_text("#!/usr/bin/env bash\n: > \"$SECOND_PROCESS_STARTED\"\nexec \"$WRAPPER\" --force\n", encoding="utf-8")
            second.chmod(0o755)
            environment = os.environ | {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "XDG_STATE_HOME": str(directory / "state"),
                "PROCESSOR_TRACE": str(trace),
                "PROCESSOR_RELEASE": str(release),
                "SECOND_PROCESS_STARTED": str(second_process_started),
                "SECOND_STOCK_STARTED": str(second_stock_started),
                "WRAPPER": str(wrapper),
                "FLOCK_TRACE": str(flock_trace),
                "REAL_FLOCK": real_flock,
            }
            first = subprocess.Popen([str(wrapper), "--limits-only"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            self.wait_for(trace.exists)
            self.assertEqual(trace.read_text(encoding="utf-8"), "old\n")
            second = subprocess.Popen([str(second)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            self.wait_for(second_process_started.exists)
            self.assertTrue(second_process_started.exists())
            self.wait_for(lambda: flock_trace.exists() and len(flock_trace.read_text(encoding="utf-8").splitlines()) == 2)
            self.assertFalse(second_stock_started.exists())
            self.assertEqual(trace.read_text(encoding="utf-8"), "old\n")
            release.touch()
            self.assertEqual(first.communicate(timeout=5)[0], "")
            self.assertEqual(second.communicate(timeout=5)[0], "")
            self.assertEqual(json.loads((directory / "state/omarchy/agents/usage/codex.json").read_text(encoding="utf-8")), {"id": "codex", "version": "new"})

    def test_default_no_flag_publication_follows_a_symlinked_usage_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            omarchy_bin = directory / "omarchy/bin"
            omarchy_bin.mkdir(parents=True)
            collector = omarchy_bin / "omarchy-agent-usage-codex"
            collector.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            collector.chmod(0o755)
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\nmkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\nprintf '%s' '{\"id\":\"codex\"}' > \"$XDG_STATE_HOME/omarchy/agents/usage/codex.json\"\nchmod 640 \"$XDG_STATE_HOME/omarchy/agents/usage/codex.json\"\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            state_home = directory / "state"
            target_usage = directory / "target-usage"
            target_usage.mkdir()
            usage_link = state_home / "omarchy/agents/usage"
            usage_link.parent.mkdir(parents=True)
            usage_link.symlink_to(target_usage, target_is_directory=True)
            result = subprocess.run([str(wrapper)], text=True, capture_output=True, env=os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(state_home), "OMARCHY_PATH": str(directory / "omarchy")})
            self.assertEqual(result.returncode, 0)
            self.assertEqual((target_usage / "codex.json").stat().st_mode & 0o777, 0o640)

    def test_serializes_non_codex_refreshes(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "mkdir -p \"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "version=old; [[ $* == *--force* ]] && version=new\n"
                "if [[ $version == old ]]; then : > \"$FIRST_STOCK_STARTED\"; while [[ ! -e $RELEASE ]]; do :; done; else : > \"$SECOND_STOCK_STARTED\"; fi\n"
                "printf '{\"id\":\"claude\",\"version\":\"%s\"}' \"$version\" > \"$XDG_STATE_HOME/omarchy/agents/usage/claude.json\"\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            real_flock = self.install_flock_shim(bin_dir)
            flock_trace = directory / "flock-trace"
            first_started = directory / "first-stock-started"
            second_process_started = directory / "second-process-started"
            second_started = directory / "second-stock-started"
            release = directory / "release"
            second_refresh = directory / "second-refresh"
            second_refresh.write_text("#!/usr/bin/env bash\n: > \"$SECOND_PROCESS_STARTED\"\nexec \"$WRAPPER\" claude --force\n", encoding="utf-8")
            second_refresh.chmod(0o755)
            environment = os.environ | {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "XDG_STATE_HOME": str(directory / "state"),
                "FIRST_STOCK_STARTED": str(first_started),
                "SECOND_PROCESS_STARTED": str(second_process_started),
                "SECOND_STOCK_STARTED": str(second_started),
                "RELEASE": str(release),
                "WRAPPER": str(wrapper),
                "FLOCK_TRACE": str(flock_trace),
                "REAL_FLOCK": real_flock,
            }
            first = subprocess.Popen([str(wrapper), "claude"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            self.wait_for(first_started.exists)
            self.assertTrue(first_started.exists())
            second = subprocess.Popen([str(second_refresh)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
            self.wait_for(second_process_started.exists)
            self.assertTrue(second_process_started.exists())
            self.wait_for(lambda: flock_trace.exists() and len(flock_trace.read_text(encoding="utf-8").splitlines()) == 2)
            self.assertFalse(second_started.exists())
            release.touch()
            first.communicate(timeout=5)
            second.communicate(timeout=5)
            self.assertEqual(json.loads((directory / "state/omarchy/agents/usage/claude.json").read_text(encoding="utf-8")), {"id": "claude", "version": "new"})

    def test_failed_enrichment_skips_codex_but_publishes_other_selected_records(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "usage=\"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "mkdir -p \"$usage\"\n"
                "printf '%s' '{\"id\":\"codex\",\"stock\":true}' > \"$usage/codex.json\"\n"
                "printf '%s' '{\"id\":\"claude\",\"stock\":true}' > \"$usage/claude.json\"\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            state_home = directory / "state"
            real_record = state_home / "omarchy/agents/usage/codex.json"
            claude_record = state_home / "omarchy/agents/usage/claude.json"
            real_record.parent.mkdir(parents=True)
            real_record.write_text('{"id":"codex","previous":true}', encoding="utf-8")
            claude_record.write_text('{"id":"claude","previous":true}', encoding="utf-8")
            result = subprocess.run(
                [str(wrapper), "codex", "claude"], text=True, capture_output=True,
                env=os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(state_home)},
            )
            self.assertEqual(result.returncode, 9)
            self.assertEqual(json.loads(real_record.read_text(encoding="utf-8")), {"id": "codex", "previous": True})
            self.assertEqual(json.loads(claude_record.read_text(encoding="utf-8")), {"id": "claude", "stock": True})
            self.assertEqual([path for path in real_record.parent.glob(".agent-usage-update.*") if path.is_dir()], [])

    def test_stock_partial_failure_publishes_available_selected_records(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            (directory / "codex_accounts.py").write_text(
                "import json, sys\n"
                "with open(sys.argv[2], encoding='utf-8') as handle: record = json.load(handle)\n"
                "record['accounts'] = [{'label': 'Enriched'}]\n"
                "with open(sys.argv[2], 'w', encoding='utf-8') as handle: json.dump(record, handle)\n",
                encoding="utf-8",
            )
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "usage=\"$XDG_STATE_HOME/omarchy/agents/usage\"\n"
                "mkdir -p \"$usage\"\n"
                "printf '%s' '{\"id\":\"codex\"}' > \"$usage/codex.json\"\n"
                "printf '%s' '{\"id\":\"claude\"}' > \"$usage/claude.json\"\n"
                "exit 7\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            state_home = directory / "state"
            result = subprocess.run([str(wrapper), "codex", "claude"], text=True, capture_output=True, env=os.environ | {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": str(state_home)})
            usage = state_home / "omarchy/agents/usage"
            self.assertEqual(result.returncode, 7)
            self.assertEqual(json.loads((usage / "codex.json").read_text(encoding="utf-8")), {"id": "codex", "accounts": [{"label": "Enriched"}]})
            self.assertEqual(json.loads((usage / "claude.json").read_text(encoding="utf-8")), {"id": "claude"})
    def run_wrapper(self, arguments=(), stock_status=0, processor_status=0, child_diagnostics=False):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            wrapper = directory / "agent-usage-update"
            processor = directory / "codex_accounts.py"
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            shutil.copy2(ROOT / "agent-usage-update", wrapper)
            wrapper.chmod(0o755)
            processor.write_text(
                "import json, os, sys\n"
                "with open(os.environ['PROCESSOR_TRACE'], 'w') as handle:\n"
                "    json.dump(sys.argv[1:], handle)\n"
                "if os.environ['CHILD_DIAGNOSTICS'] == '1': print('processor-secret@example.test', file=sys.stderr)\n"
                "raise SystemExit(int(os.environ['PROCESSOR_STATUS']))\n",
                encoding="utf-8",
            )
            stock = bin_dir / "omarchy-agent-usage-update"
            stock.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$STOCK_TRACE\"\n"
                "[[ $CHILD_DIAGNOSTICS == 1 ]] && printf 'stock Bearer secret-token\\n' >&2\n"
                "exit \"$STOCK_STATUS\"\n",
                encoding="utf-8",
            )
            stock.chmod(0o755)
            stock_trace = directory / "stock-trace"
            processor_trace = directory / "processor-trace"
            state_home = directory / "state"
            environment = os.environ | {
                "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "XDG_STATE_HOME": str(state_home),
                "STOCK_TRACE": str(stock_trace),
                "STOCK_STATUS": str(stock_status),
                "PROCESSOR_TRACE": str(processor_trace),
                "PROCESSOR_STATUS": str(processor_status),
                "CHILD_DIAGNOSTICS": "1" if child_diagnostics else "0",
            }
            result = subprocess.run([str(wrapper), *arguments], text=True, capture_output=True, env=environment)
            stock_arguments = stock_trace.read_text(encoding="utf-8").splitlines()
            processor_arguments = json.loads(processor_trace.read_text(encoding="utf-8")) if processor_trace.exists() else None
            return result, stock_arguments, processor_arguments, state_home

    def test_forwards_all_stock_flags_and_provider_selections(self):
        result, stock_arguments, processor_arguments, state_home = self.run_wrapper(
            ("--force", "--limits-only", "--except", "fireworks", "codex")
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(stock_arguments, ["--force", "--limits-only", "--except", "fireworks", "codex"])
        self.assertEqual(processor_arguments[0], "--record")
        self.assertTrue(processor_arguments[1].endswith("/state/omarchy/agents/usage/codex.json"))
        self.assertNotIn(str(state_home / "omarchy/agents/usage/codex.json"), processor_arguments[1])

    def test_skips_postprocessor_when_stock_selection_excludes_codex(self):
        for arguments in (("--except", "codex"), ("claude",)):
            with self.subTest(arguments=arguments):
                result, stock_arguments, processor_arguments, _ = self.run_wrapper(arguments)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(stock_arguments, list(arguments))
                self.assertIsNone(processor_arguments)

    def test_no_explicit_provider_selects_codex(self):
        result, _, processor_arguments, _ = self.run_wrapper(("--force",))

        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(processor_arguments)

    def test_returns_stock_failure_before_postprocessor_failure(self):
        result, _, processor_arguments, _ = self.run_wrapper(("codex",), stock_status=7, processor_status=9)

        self.assertEqual(result.returncode, 7)
        self.assertIsNotNone(processor_arguments)

    def test_returns_postprocessor_failure_when_stock_succeeds(self):
        result, _, processor_arguments, _ = self.run_wrapper(("codex",), processor_status=9)

        self.assertEqual(result.returncode, 9)
        self.assertIsNotNone(processor_arguments)

    def test_reports_compact_generic_failures_without_child_diagnostics(self):
        result, _, processor_arguments, _ = self.run_wrapper(
            ("codex",), stock_status=7, processor_status=9, child_diagnostics=True
        )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.splitlines(),
            ["agent-usage-update: stock update failed", "agent-usage-update: Codex account update failed"],
        )
        self.assertNotIn("secret", result.stderr.lower())
        self.assertIsNotNone(processor_arguments)


class QmlContractTests(unittest.TestCase):
    def test_wrapper_declares_silent_stock_forwarding_and_selection(self):
        wrapper = (ROOT / "agent-usage-update").read_text(encoding="utf-8")

        self.assertIn('arguments=("$@")', wrapper)
        self.assertIn('XDG_STATE_HOME="$stage_state" omarchy-agent-usage-update "${arguments[@]}" >/dev/null 2>&1', wrapper)
        self.assertIn('[[ ${1:-} == codex ]] && codex_excluded=1', wrapper)
        self.assertIn('[[ $1 == codex ]] && codex_selected=1', wrapper)
        self.assertIn('python3 "$script_dir/codex_accounts.py" --record "$stage_usage/codex.json" >/dev/null 2>&1', wrapper)
        self.assertIn('mv "$stage_usage/$provider.json" "$usage_dir/$provider.json"', wrapper)
        self.assertIn('for collector in "$OMARCHY_PATH"/bin/omarchy-agent-usage-*; do', wrapper)
        self.assertNotIn('${OMARCHY_PATH:-/usr/share/omarchy}', wrapper)

    def test_main_routes_refresh_and_sanitizes_account_display_data(self):
        main = (ROOT / "Main.qml").read_text(encoding="utf-8")

        self.assertIn('Qt.resolvedUrl("agent-usage-update")', main)
        self.assertIn('if (kind === "limits") command.push("--limits-only")', main)
        self.assertIn("function codexAccountsValue(raw)", main)
        self.assertIn('accounts: String(record.id) === "codex" ? codexAccountsValue(record.accounts) : null', main)
        self.assertIn("Array.isArray(p.accounts) && p.accounts.length > 0", main)

    def test_panel_uses_account_cards_and_account_alarm_semantics(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")

        self.assertIn('moduleName: "rnoh.agents"', panel)
        self.assertIn('ipcTarget: "rnoh.agents"', panel)
        self.assertIn('p.providerId === "codex" && Array.isArray(p.accounts) && p.accounts.length > 0', panel)
        self.assertIn('p.accounts.length + " account"', panel)
        self.assertIn("component CodexAccountCard", panel)
        self.assertIn("component CodexAccountLimitRow", panel)
        self.assertIn("Number(accountLimitRow.limit.usedPercent) / 100", panel)
        self.assertIn("Number(limits[j].usedPercent) >= 90", panel)
        self.assertIn("!root.enrichedCodex(root.provider) && root.limits.length > 0", panel)
        self.assertIn('"Not reported"', panel)
        self.assertIn('"Also tracked upstream: "', panel)
        self.assertIn('"Stale · last updated " + accountCard.account.lastSuccessAt', panel)

    def test_readme_describes_the_sanitized_codex_account_display(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("omarchy-shell rnoh.agents", readme)
        self.assertIn("omarchy bar set rnoh.agents", readme)
        self.assertNotIn("omarchy.agents", readme)
        self.assertIn("Also tracked upstream:", readme)
        self.assertIn("Stale · last updated", readme)


if __name__ == "__main__":
    unittest.main()
