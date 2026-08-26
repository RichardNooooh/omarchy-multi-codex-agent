import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex_accounts.py"


class CodexAccountsCliTests(unittest.TestCase):
    def run_processor(self, directory, stock, stdout, returncode=0, cache=True, now="2026-08-26T12:00:00Z", environment=None):
        record = directory / "codex.json"
        command = directory / "limits.py"
        cache_path = directory / "cache.json"
        record.write_text(json.dumps(stock), encoding="utf-8")
        command.write_text(
            "import sys\n"
            f"sys.stdout.write({stdout!r})\n"
            f"raise SystemExit({returncode})\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--record",
                str(record),
                "--limits-command",
                sys.executable,
                str(command),
                "--now",
                now,
                *( ["--cache", str(cache_path)] if cache else []),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=(os.environ | environment) if environment else None,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(record.read_text(encoding="utf-8")), cache_path

    def test_preserves_stock_fields_and_writes_normalized_success(self):
        payload = {
            "accounts": [
                {
                    "index": 7,
                    "label": "Work",
                    "planType": "pro",
                    "limits": [
                        {
                            "name": "five_hour",
                            "windowMinutes": 300,
                            "usedPercent": 23.5,
                            "resetAtMs": 1787752800000,
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stock = {
                "theme": "dark",
                "storagePath": "/stock-owned/path",
                "nested": {"keep": True, "credentials": {"token": "stock-owned-value"}},
            }
            output, _ = self.run_processor(directory, stock, json.dumps(payload), returncode=7)
            self.assertEqual(stat_mode(directory / "codex.json"), 0o600)

        self.assertEqual(output["theme"], "dark")
        self.assertEqual(output["storagePath"], "/stock-owned/path")
        self.assertEqual(output["nested"], {"keep": True, "credentials": {"token": "stock-owned-value"}})
        self.assertEqual(
            output["accounts"],
            [
                {
                    "index": 7,
                    "label": "Work",
                    "planType": "pro",
                    "limits": [
                        {"windowMinutes": 300, "usedPercent": 23.5, "resetAtMs": 1787752800000}
                    ],
                    "additionalLimitNames": [],
                    "lastSuccessAt": "2026-08-26T12:00:00Z",
                    "stale": False,
                    "error": "",
                    "warning": "",
                }
            ],
        )

    def test_schema_changed_json_is_a_total_failure_with_a_sanitized_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            output, _ = self.run_processor(Path(temp), {"stock": True}, json.dumps({"accounts": {"changed": True}}))

        self.assertTrue(output["stock"])
        self.assertEqual(output["accounts"], [])

    def test_sanitizes_arbitrary_accounts_labels_meters_and_sensitive_values(self):
        payload = {
            "accounts": [
                {"label": "", "limits": [{"name": "weekly", "windowMinutes": 10080, "usedPercent": 120, "resetAtMs": -1}]},
                {"label": "A", "planType": 2, "limits": [{"name": "extra", "windowMinutes": 42, "usedPercent": 2}, {"name": "extra", "windowMinutes": 43}, {"name": "burst", "windowMinutes": 44}], "email": "private@example.test", "credentials": "secret"},
                {"label": "A", "error": "bad\x00token", "limits": [{"name": "ignored", "windowMinutes": 300, "usedPercent": 4, "resetAtMs": 1787752800000}]},
                {"label": "Account 9", "limits": []},
            ],
            "rateLimitResetTimes": {"email": "private@example.test"},
        }
        with tempfile.TemporaryDirectory() as temp:
            output, cache_path = self.run_processor(Path(temp), {"storagePath": "/secret"}, json.dumps(payload))
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(stat_mode(cache_path), 0o600)

        self.assertEqual(len(output["accounts"]), 4)
        self.assertEqual(output["accounts"][0]["label"], "Account 1")
        self.assertEqual(output["accounts"][0]["limits"], [{"windowMinutes": 10080, "usedPercent": 100, "resetAtMs": None}])
        self.assertEqual(output["accounts"][1]["additionalLimitNames"], ["extra", "burst"])
        self.assertIn("duplicate", output["accounts"][1]["warning"])
        self.assertEqual(output["accounts"][2]["error"], "badtoken")
        self.assertEqual(output["accounts"][2]["limits"], [])
        self.assertIn("not cache", output["accounts"][3]["warning"])
        self.assertEqual([item["label"] for item in cached["accounts"]], [])
        self.assert_no_sensitive(output["accounts"])
        self.assert_no_sensitive(cached)

    def test_partial_failure_uses_unexpired_unique_label_cache(self):
        fresh = {"accounts": [{"label": "Work", "limits": [{"windowMinutes": 300, "usedPercent": 1, "resetAtMs": 1787851200000}]}]}
        failed = {"accounts": [{"label": "Work", "error": "network down"}]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_processor(directory, {}, json.dumps(fresh))
            output, _ = self.run_processor(directory, {}, json.dumps(failed), now="2026-08-26T13:00:00Z")

        item = output["accounts"][0]
        self.assertTrue(item["stale"])
        self.assertEqual(item["limits"][0]["usedPercent"], 1)
        self.assertEqual(item["error"], "network down")
        self.assertEqual(item["lastSuccessAt"], "2026-08-26T12:00:00Z")

    def test_rejects_untrusted_cache_records_before_a_total_failure_can_use_them(self):
        unsafe_cache = {
            "version": 1,
            "accounts": [{
                "index": 0,
                "label": "Work",
                "planType": "Pro",
                "limits": [{"windowMinutes": 300, "usedPercent": float("nan"), "resetAtMs": None}],
                "additionalLimitNames": [],
                "lastSuccessAt": "2026-08-26T12:00:00Z",
                "stale": False,
                "error": "",
                "warning": "",
                "token": "Bearer leaked-token",
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "cache.json").write_text(json.dumps(unsafe_cache), encoding="utf-8")
            output, _ = self.run_processor(directory, {}, json.dumps({"error": "offline"}))

        self.assertEqual(output["accounts"], [])

    def test_sanitizes_live_display_metadata_and_failed_timestamps(self):
        payload = {"accounts": [
            {
                "label": "Work",
                "planType": "Pro",
                "limits": [{"name": "extra-%d" % index, "windowMinutes": index, "usedPercent": 1} for index in range(1, 12)],
            },
            {
                "label": "alice@example.test",
                "planType": "/home/alice/private-plan",
                "error": "alice@example.test failed at /home/alice/.codex with Bearer abc.def.ghi\x00",
                "limits": [{"name": "refresh_token=secret", "windowMinutes": 42, "usedPercent": 1}],
            },
        ]}
        with tempfile.TemporaryDirectory() as temp:
            output, cache_path = self.run_processor(Path(temp), {}, json.dumps(payload))
            cached = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(output["accounts"][0]["label"], "Work")
        self.assertEqual(len(output["accounts"][0]["additionalLimitNames"]), 8)
        self.assertEqual(output["accounts"][1]["label"], "Account 2")
        self.assertEqual(output["accounts"][1]["planType"], "Plan unavailable")
        self.assertEqual(output["accounts"][1]["error"], "limits unavailable")
        self.assertEqual(output["accounts"][1]["lastSuccessAt"], "")
        self.assertEqual(len(cached["accounts"][0]["additionalLimitNames"]), 8)
        self.assert_no_sensitive(output)
        self.assert_no_sensitive(cached)

    def test_rejects_punctuation_adjacent_paths_and_identifier_values_everywhere(self):
        sensitive = [
            "config=/home/rnoh/.codex",
            "path=(/home/rnoh/.codex)",
            "account_id=acct_7f8e9d0c1b2a3f4e",
            "workspaceId=ws_7f8e9d0c1b2a3f4e",
            "organization_id=org_7f8e9d0c1b2a3f4e",
            "storage-id=store_7f8e9d0c1b2a3f4e",
            "123e4567-e89b-12d3-a456-426614174000",
            "Bearer abc.def.ghi",
            "refresh_token=secret-value",
            "alice@example.test",
            "a8F3kL9pQ2rT7vX1zC4mN6sW0yB5dH8j",
        ]
        payload = {"accounts": [
            {
                "label": "Work",
                "planType": "Personal",
                "limits": [{"name": "Burst", "windowMinutes": 42, "usedPercent": 1}],
            },
            *[
                {
                    "label": value,
                    "planType": value,
                    "limits": [{"name": value, "windowMinutes": 42, "usedPercent": 1}],
                    "error": "request failed: " + value,
                }
                for value in sensitive
            ],
        ]}
        with tempfile.TemporaryDirectory() as temp:
            output, cache_path = self.run_processor(Path(temp), {}, json.dumps(payload))
            cached = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(output["accounts"][0]["label"], "Work")
        self.assertEqual(output["accounts"][0]["planType"], "Personal")
        for item in output["accounts"][1:]:
            self.assertEqual(item["label"], "Account " + str(item["index"] + 1))
            self.assertEqual(item["planType"], "Plan unavailable")
            self.assertEqual(item["additionalLimitNames"], ["Additional limit"])
            self.assertEqual(item["error"], "limits unavailable")
        self.assert_no_sensitive(output)
        self.assert_no_sensitive(cached)

    def test_uses_no_default_cache_without_a_runtime_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output, cache_path = self.run_processor(
                directory,
                {},
                json.dumps({"accounts": [{"label": "Work", "limits": []}]}),
                cache=False,
                environment={"XDG_RUNTIME_DIR": ""},
            )

        self.assertEqual(output["accounts"][0]["label"], "Work")
        self.assertFalse(cache_path.exists())

    def test_total_failure_restores_only_unexpired_cache_and_removes_expired_meters(self):
        payload = {"accounts": [
            {"label": "ResetFirst", "limits": [{"windowMinutes": 300, "usedPercent": 1, "resetAtMs": 1787745600000}]},
            {"label": "DayFirst", "limits": [{"windowMinutes": 300, "usedPercent": 2, "resetAtMs": 1787851200000}]},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_processor(directory, {}, json.dumps(payload), now="2026-08-25T13:00:00Z")
            output, _ = self.run_processor(directory, {}, "not json", 1)

        self.assertEqual([item["label"] for item in output["accounts"]], ["ResetFirst", "DayFirst"])
        self.assertEqual(output["accounts"][0]["limits"], [])
        self.assertEqual(output["accounts"][1]["limits"][0]["usedPercent"], 2)
        self.assertTrue(all(item["stale"] for item in output["accounts"]))
        self.assertTrue(all(item["error"] for item in output["accounts"]))

    def test_total_failure_removes_limits_after_24_hours_even_before_reset(self):
        payload = {"accounts": [{
            "label": "Work",
            "limits": [{"windowMinutes": 300, "usedPercent": 2, "resetAtMs": 1787851200000}],
        }]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_processor(directory, {}, json.dumps(payload), now="2026-08-25T12:00:00Z")
            output, _ = self.run_processor(directory, {}, "not json", 1, now="2026-08-26T12:00:00Z")

        self.assertTrue(output["accounts"][0]["stale"])
        self.assertEqual(output["accounts"][0]["limits"], [])

    def test_total_failure_removes_limits_at_the_earliest_reset(self):
        payload = {"accounts": [{
            "label": "Work",
            "limits": [
                {"windowMinutes": 300, "usedPercent": 2, "resetAtMs": 1787745600000},
                {"windowMinutes": 10080, "usedPercent": 3, "resetAtMs": 1787851200000},
            ],
        }]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_processor(directory, {}, json.dumps(payload), now="2026-08-26T06:00:00Z")
            output, _ = self.run_processor(directory, {}, "not json", 1, now="2026-08-26T12:00:00Z")

        self.assertEqual(output["accounts"][0]["limits"], [])

    def test_valid_source_replaces_removed_cache_accounts_but_retains_current_failures(self):
        initial = {"accounts": [
            {"label": "A", "limits": [{"windowMinutes": 300, "usedPercent": 1, "resetAtMs": 1787851200000}]},
            {"label": "B", "limits": [{"windowMinutes": 300, "usedPercent": 2, "resetAtMs": 1787851200000}]},
        ]}
        partial = {"accounts": [
            {"label": "A", "limits": [{"windowMinutes": 300, "usedPercent": 3, "resetAtMs": 1787851200000}]},
            {"label": "B", "error": "offline"},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_processor(directory, {}, json.dumps(initial))
            output, _ = self.run_processor(directory, {}, json.dumps(partial), now="2026-08-26T13:00:00Z")
            self.assertEqual(output["accounts"][1]["limits"][0]["usedPercent"], 2)
            output, _ = self.run_processor(directory, {}, json.dumps({"accounts": [{"label": "A", "limits": []}]}), now="2026-08-26T14:00:00Z")
            output, _ = self.run_processor(directory, {}, "not json", 1, now="2026-08-26T15:00:00Z")

        self.assertEqual([item["label"] for item in output["accounts"]], ["A"])

    def assert_no_sensitive(self, value):
        sensitive_key = re.compile(r"email|storagepath|credential|accountid|ratelimitresettimes|token", re.IGNORECASE)
        sensitive_value = re.compile(r"[^\s@/]+@[^\s@/]+\.[^\s@/]+|(?:~|/)[^\s]*|\b(?:account|workspace|organization|org|storage)[_-]?(?:id|identifier)?\s*[:=]\s*\S+|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\bbearer\b|\b(?:refresh|access)[_ -]?token\b|\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|\b(?=[A-Za-z0-9_-]{24,}\b)(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b", re.IGNORECASE)

        def visit(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    self.assertIsInstance(key, str)
                    self.assertIsNone(sensitive_key.search(key), key)
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str):
                self.assertIsNone(sensitive_value.search(item), item)
                self.assertTrue(all(character >= " " and character != "\x7f" for character in item), item)

        visit(value)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
