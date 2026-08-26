import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateWrapperTests(unittest.TestCase):
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
        self.assertEqual(processor_arguments, ["--record", str(state_home / "omarchy/agents/usage/codex.json")])

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
        self.assertIn('omarchy-agent-usage-update "${arguments[@]}" >/dev/null 2>&1', wrapper)
        self.assertIn('[[ ${1:-} == codex ]] && codex_excluded=1', wrapper)
        self.assertIn('[[ $1 == codex ]] && codex_selected=1', wrapper)
        self.assertIn('python3 "$script_dir/codex_accounts.py" --record "$state_home/omarchy/agents/usage/codex.json" >/dev/null 2>&1', wrapper)

    def test_main_routes_refresh_and_sanitizes_account_display_data(self):
        main = (ROOT / "Main.qml").read_text(encoding="utf-8")

        self.assertIn('Qt.resolvedUrl("agent-usage-update")', main)
        self.assertIn("function codexAccountsValue(raw)", main)
        self.assertIn('accounts: String(record.id) === "codex" ? codexAccountsValue(record.accounts) : null', main)
        self.assertIn("Array.isArray(p.accounts) && p.accounts.length > 0", main)

    def test_panel_uses_account_cards_and_account_alarm_semantics(self):
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")

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

        self.assertIn("Also tracked upstream:", readme)
        self.assertIn("Stale · last updated", readme)


if __name__ == "__main__":
    unittest.main()
