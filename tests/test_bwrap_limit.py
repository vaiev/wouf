"""Systemd scope argument escaping and capability caching, without systemd."""

import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents" / "bin"))
from teamlib import bwrap


class LimitArgvTest(unittest.TestCase):
    def setUp(self):
        scope = patch.object(bwrap, "scope_usable", return_value=True)
        scope.start()
        self.addCleanup(scope.stop)
        self.argv = ["bwrap", "--bind", "/tmp/$HOME", "/bot", "--", "bash", "-c",
                     'printf "%s\\n" "${line#*=}" "$HOME" "$$" "$!"']

    def test_supported_option_keeps_command_arguments_literal(self):
        with patch.object(bwrap, "scope_no_expand_supported", return_value=True):
            result = bwrap.limit_argv(self.argv, "4g", "2")
        self.assertIn("--expand-environment=no", result[:result.index("env")])
        self.assertEqual(result[result.index("env"):], ["env", "-i", *self.argv])

    def test_older_systemd_escapes_every_dollar_in_command_arguments(self):
        original = self.argv.copy()
        with patch.object(bwrap, "scope_no_expand_supported", return_value=False):
            result = bwrap.limit_argv(self.argv, "4g", "2")
        self.assertNotIn("--expand-environment=no", result)
        self.assertEqual(result[result.index("env"):],
                         ["env", "-i", "bwrap", "--bind", "/tmp/$$HOME", "/bot", "--", "bash", "-c",
                          'printf "%s\\n" "$${line#*=}" "$$HOME" "$$$$" "$$!"'])
        self.assertEqual(self.argv, original)

    def test_no_scope_returns_the_original_arguments_without_probing(self):
        with patch.object(bwrap, "scope_usable", return_value=False), \
                patch.object(bwrap, "scope_no_expand_supported") as probe, patch.object(bwrap, "say") as say:
            self.assertIs(bwrap.limit_argv(self.argv, "4g", "2"), self.argv)
        probe.assert_not_called()
        say.assert_called_once_with("team: no CPU, memory or process limit on this computer", err=True)

    def test_help_is_probed_once_for_both_supported_and_missing_options(self):
        self.addCleanup(bwrap.scope_no_expand_supported.cache_clear)
        for supported, help_text in ((True, "  --expand-environment=BOOL  Expand variables\n"),
                                     (False, "  --scope  Run in a scope\n")):
            with self.subTest(supported=supported):
                bwrap.scope_no_expand_supported.cache_clear()
                with patch.object(bwrap.subprocess, "run",
                                  return_value=CompletedProcess([], 0, stdout=help_text)) as run:
                    first = bwrap.limit_argv(self.argv, "4g", "2")
                    second = bwrap.limit_argv(self.argv, "4g", "2")
                self.assertEqual(first, second)
                self.assertEqual("--expand-environment=no" in first, supported)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ["systemd-run", "--help"])


if __name__ == "__main__":
    unittest.main()
