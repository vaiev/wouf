"""scripts/mdlint.py: the markdownlint rules of the pre-gate, run on temp files and on this repository."""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import TIME_FACTOR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mdlint.py"
ALL_ON = "config:\n  default: true\n"

# Per rule: (failing snippet, its finding lines, passing snippet).
CASES = {
    "MD009": ("Text \nmore   \n", [1, 2], "Line break  \nnext\n"),
    "MD010": ("Text\twith a tab\n", [1], "Text with spaces\n"),
    "MD012": ("One\n\n\nTwo\n", [3], "One\n\nTwo\n"),
    "MD022": ("Text\n# Heading\nText\n", [2, 2], "Text\n\n# Heading\n\nText\n"),
    "MD031": ("Text\n```sh\nls\n```\nText\n", [2, 4], "Text\n\n```sh\nls\n```\n\nText\n"),
    "MD032": ("Text\n- one\n- two\n***\n", [2, 3], "Text\n\n- one\n- two\n\n***\n"),
    "MD047": ("Text", [1], "Text\n"),
}


class MdlintTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.config(ALL_ON)

    def config(self, text):
        (self.dir / ".markdownlint-cli2.yaml").write_text(text)

    def run_on(self, text, name="doc.md"):
        (self.dir / name).write_bytes(text.encode("utf-8"))
        return subprocess.run([sys.executable, str(SCRIPT), name], cwd=self.dir, text=True, capture_output=True)

    def findings(self, text):
        result = self.run_on(text)
        return [(int(line.split()[0].split(":")[1]), line.split()[1]) for line in result.stdout.splitlines()]

    def test_each_rule_fails_and_passes(self):
        for rule, (bad, lines, good) in CASES.items():
            with self.subTest(rule=rule):
                self.assertEqual([(n, rule) for n in lines], self.findings(bad))
                self.assertEqual([], self.findings(good))

    def test_a_rule_set_to_false_is_skipped(self):
        self.config("config:\n  default: true\n  MD012: false # off\n  MD013: false\n")
        self.assertEqual([], self.findings(CASES["MD012"][0]))
        self.assertEqual([(1, "MD010")], self.findings(CASES["MD010"][0]))
        self.config("config:\n  default: false\n  MD010: true\n")
        self.assertEqual([(1, "MD010")], self.findings("Tab\there\n\n\n# H\n"))
        self.config("config:\n  no-hard-tabs: false\n")
        self.assertEqual([], self.findings(CASES["MD010"][0]))

    def test_fenced_code(self):
        # Inside a fence: no heading, no list, blank lines and trailing spaces allowed; tabs still count.
        text = "Text\n\n```text\n# not a heading\n- not a list\n\n\nend   \n\tindented\n```\n\nText\n"
        self.assertEqual([(9, "MD010")], self.findings(text))
        self.assertEqual([], self.findings("Text\n\n~~~\n```\n~~~\n\nText\n"))
        self.assertEqual([], self.findings("- item\n\n  ```sh\n  ls\n  ```\n\nText\n"))
        self.assertEqual([(2, "MD031"), (3, "MD031"), (3, "MD032")], self.findings("- item\n  ```sh\n  ```\nText\n"))
        self.assertEqual([(6, "MD012")], self.findings("```\n\n\n```\n\n\nText\n"))

    def test_lists_end_at_the_right_line(self):
        lazy = "- one\ncontinued\n  1. nested\n     text\n- two\n\n    code\n\nText\n"
        self.assertEqual([], self.findings(lazy))
        self.assertEqual([], self.findings("Text\n2. no list\n"))
        self.assertEqual([(1, "MD032"), (2, "MD022")], self.findings("- one\n# Heading\n\nText\n"))
        self.assertEqual([(1, "MD032"), (2, "MD032")], self.findings("- one\n* other list\n"))
        self.assertEqual([], self.findings("- one\n<!-- comment -->\nText\n"))

    def test_headings_and_front_matter(self):
        self.assertEqual([(1, "MD022"), (5, "MD022")], self.findings("Title\n===\nText\n\nNext\n---\nText\n"))
        self.assertEqual([(4, "MD022")], self.findings("Title\n===\n\nNext\n---\nText\n"))
        self.assertEqual([], self.findings("Text\n\n---\n\n#hashtag\n"))
        front = "---\nname: x\n---\n\n\n# Heading\n\nText  \n"
        self.assertEqual([], self.findings(front))
        self.assertEqual([(9, "MD009")], self.findings(front + "bad \n"))

    def test_md012_skips_the_end_after_the_final_newline(self):
        self.assertEqual([(2, "MD047")], self.findings("Text\n\n"))
        self.assertEqual([(3, "MD012")], self.findings("One\n\n\nTwo\n"))
        self.assertEqual([(3, "MD012"), (3, "MD047")], self.findings("Text\n\n\n"))

    def test_md047_wants_exactly_one_final_newline(self):
        self.config("config:\n  default: false\n  MD047: true\n")
        for text, lines in (("Text\n\n", [2]), ("Text", [1]), ("  ", [1]), ("Text\n", []), ("", [])):
            with self.subTest(text=text):
                self.assertEqual([(n, "MD047") for n in lines], self.findings(text))

    def test_md022_in_block_quotes(self):
        self.assertEqual([(2, "MD022"), (2, "MD022")], self.findings("> Text\n> # Heading\n> Text\n"))
        self.assertEqual([(2, "MD022"), (2, "MD022")], self.findings("> > Text\n> > # Heading\n> > Text\n"))
        self.assertEqual([], self.findings("> Text\n>\n> # Heading\n>\n> Text\n"))
        self.assertEqual([], self.findings("> > Text\n> >\n> > # Heading\n> >\n> > Text\n"))

    def test_md031_in_block_quotes(self):
        self.assertEqual([(2, "MD031"), (4, "MD031")], self.findings("> Text\n> ```\n> code\n> ```\n> Text\n"))
        nested = "> > Text\n> > ```\n> > code\n> > ```\n> > Text\n"
        self.assertEqual([(2, "MD031"), (4, "MD031")], self.findings(nested))
        self.assertEqual([], self.findings("> Text\n>\n> ```\n> code\n> ```\n>\n> Text\n"))
        self.assertEqual([], self.findings("> > Text\n> >\n> > ```\n> > # code\n> > ```\n> >\n> > Text\n"))

    def test_md032_in_block_quotes(self):
        self.assertEqual([(2, "MD032"), (2, "MD032")], self.findings("> Text\n> - item\n> ***\n"))
        self.assertEqual([(2, "MD032"), (2, "MD032")], self.findings("> > Text\n> > - item\n> > ***\n"))
        self.assertEqual([], self.findings("> Text\n>\n> - item\n>\n> ***\n"))
        self.assertEqual([], self.findings("> > Text\n> >\n> > - item\n> >\n> > ***\n"))

    def test_code_in_a_quoted_fence_is_code_for_every_rule(self):
        fence = "Text\n\n```\ncode   \n \n\n\n\tend\n```\n\nText\n"
        self.assertEqual([(8, "MD010")], self.findings(fence))
        for prefix in ("> ", "> > "):
            with self.subTest(prefix=prefix):
                quoted = "".join((prefix + line if line else prefix.rstrip(" ")) + "\n"
                                 for line in fence.split("\n")[:-1])
                self.assertEqual([(8, "MD010")], self.findings(quoted))
        # Blank code lines that keep the marker's space, and indented code in a quote.
        self.assertEqual([], self.findings("> ```\n> code   \n> \n> \n> ```\n"))
        self.assertEqual([], self.findings("> Text\n>\n>     code   \n>     \n>     end\n>\n> Text\n"))
        self.assertEqual([], self.findings("> - item\n>\n>   ```\n>   code   \n>   ```\n"))
        # Outside the code the quote's own lines still count.
        self.assertEqual([(1, "MD009"), (6, "MD009")], self.findings("> Text \n>\n> ```\n> ```\n>\n> Text   \n"))

    def test_a_quote_inside_a_list_item_is_left_to_markdownlint(self):
        body = ["```", "code   ", "\tx", "", "", "```"]
        for head, indent in (("- ", "  "), ("* ", "  "), ("1. ", "   "), ("- item\n\n  - ", "    "),
                             ("1. item\n\n   2) ", "      ")):
            with self.subTest(head=head):
                quoted = head + "> " + body[0] + "\n" + "".join(indent + "> " + line + "\n" for line in body[1:])
                self.assertEqual([], self.findings(quoted))
                self.assertEqual([], self.findings(quoted.replace("> \n", ">\n") + "lazy text \n"))
        # The same lines outside a list still give their findings as today.
        self.assertEqual([(2, "MD009"), (3, "MD010"), (4, "MD009"), (5, "MD009")],
                         self.findings("Text\ncode   \n\tx\n> \n> \nText\n"))
        self.assertEqual([(3, "MD010")], self.findings("".join("> " + line + "\n" for line in body)))
        self.assertEqual([(3, "MD009"), (5, "MD012")], self.findings("- > text\n\ncode   \n\n\nText\n"))

    def test_quoted_list_items_stay_linear(self):
        spec = importlib.util.spec_from_file_location("mdlint", SCRIPT)
        mdlint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mdlint)
        for text in ("- > ```\n" + "  > code   \n" * 4999, "".join("- > item %d \n" % k for k in range(5000)),
                     "- " * 2500 + "> " * 2500 + "x \n", ("-   " * 2500 + "x\n") * 2):
            with self.subTest(text=text[:12]):
                start = time.monotonic()
                self.assertEqual([], mdlint.lint_text(text, set(mdlint.NAMES)))
                self.assertLess(time.monotonic() - start, 2)

    def test_md012_and_md047_read_the_real_end_after_front_matter(self):
        for text, found in (("---\na\n---\n\n", [(4, "MD047")]), ("---\na\n---", [(3, "MD047")]),
                            ("---\na\n---\n", []), ("---\na\n---\n\n\n", [(5, "MD047")]),
                            ("---\na\n---\nText\n\n", [(5, "MD047")]), ("---\na\n---\nText", [(4, "MD047")]),
                            ("---\na\n---\nText\n", []), ("---\na\n---\n\nOne\n\n\nTwo\n", [(7, "MD012")]),
                            ("---\na\n---\nText\n\n\n", [(6, "MD012"), (6, "MD047")])):
            with self.subTest(text=text):
                self.assertEqual(found, self.findings(text))

    def test_deep_nesting_needs_no_recursion(self):
        spec = importlib.util.spec_from_file_location("mdlint", SCRIPT)
        mdlint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mdlint)
        rules = set(mdlint.NAMES)
        cases = (
            ("> " * 5000 + "x\n", []),
            ("- " * 5000 + "x\n", []),
            ("> - " * 2500 + "x\n", []),
            ("".join("> " * k + "x\n" for k in range(1, 5001)), []),
            ("".join("  " * k + "- x\n" for k in range(5000)), []),
            ("> " * 5000 + "```\n" + "> " * 5000 + "code   \n", []),
        )
        # The two cases of 5000 lines up to 5000 levels deep (25 MB) take 0.6 s, 6 s on a loaded host. Their bound
        # stops a cubic reading (each depth cut again from the line start: about 2 h), not a quadratic one (one marker
        # cut per depth, 4e10 bytes copied: 2 to 6 s, as fast as a loaded host). The one-line cases keep 2 s.
        for text, found in cases:
            with self.subTest(text=text[:12]):
                start = time.monotonic()
                self.assertEqual(found, mdlint.lint_text(text, rules))
                self.assertLess(time.monotonic() - start, 30 * TIME_FACTOR if len(text) > 10 ** 6 else 2)

    def test_cli_output_and_exit_codes(self):
        result = self.run_on("Text \n\n\n# H\nText", name="a b.md")
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual(["a b.md:1 MD009 Trailing spaces [Expected: 0 or 2; Actual: 1]",
                          "a b.md:3 MD012 Multiple consecutive blank lines [Expected: 1; Actual: 2]",
                          "a b.md:4 MD022 Headings should be surrounded by blank lines [Expected: 1; Actual: 0; Below]",
                          "a b.md:5 MD047 Files should end with a single newline character"],
                         result.stdout.splitlines())
        result = self.run_on("# Fine\n")
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))
        missing = subprocess.run([sys.executable, str(SCRIPT), "absent.md"], cwd=self.dir, capture_output=True)
        self.assertEqual(2, missing.returncode)

    def test_without_arguments_only_tracked_markdown_is_checked(self):
        git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T"]
        subprocess.run(git + ["init", "-q"], cwd=self.dir, check=True)
        (self.dir / "sub").mkdir()
        (self.dir / "sub/bad.md").write_text("Text\t\n")
        (self.dir / "untracked.md").write_text("Text \n")
        (self.dir / "notes.txt").write_text("Text \n")
        subprocess.run(git + ["add", "sub/bad.md", "notes.txt"], cwd=self.dir, check=True)
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.dir, text=True, capture_output=True)
        self.assertEqual(1, result.returncode)
        self.assertEqual(["sub/bad.md:1 MD009 Trailing spaces [Expected: 0 or 2; Actual: 1]",
                          "sub/bad.md:1 MD010 Hard tabs"], result.stdout.splitlines())

    def run_without_git(self, folder):
        """mdlint without arguments in `folder`, with git kept from finding a work tree above it."""
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(folder).parent))
        return subprocess.run([sys.executable, str(SCRIPT)], cwd=folder, text=True, capture_output=True, env=env)

    def test_without_git_every_markdown_file_but_node_modules_is_checked(self):
        (self.dir / "sub").mkdir()
        (self.dir / "sub/bad.md").write_text("Text\there\n")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules/x.md").write_text("Text \n")
        (self.dir / "notes.txt").write_text("Text \n")
        result = self.run_without_git(self.dir)
        self.assertEqual((1, ["sub/bad.md:1 MD010 Hard tabs"], ""),
                         (result.returncode, result.stdout.splitlines(), result.stderr))

    def test_a_copy_of_the_repository_without_git_passes(self):
        spec = importlib.util.spec_from_file_location("mdlint", SCRIPT)
        mdlint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mdlint)
        tracked = git_markdown(ROOT)
        names = mdlint.walked(str(ROOT)) if tracked is None else tracked
        for name in names + [".markdownlint-cli2.yaml"]:
            (self.dir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.dir / name)
        result = self.run_without_git(self.dir)
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))
        if tracked is not None:
            self.assertEqual(tracked, mdlint.walked(str(self.dir)))

    def test_the_repository_passes(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))


def git_markdown(root):
    """`git ls-files '*.md'` of `root` sorted, or None when it is no git checkout."""
    try:
        result = subprocess.run(["git", "ls-files", "-z", "--", "*.md"], cwd=root, capture_output=True)
    except OSError:
        return None
    return sorted(result.stdout.decode("utf-8").split("\0")[:-1]) if result.returncode == 0 else None


if __name__ == "__main__":
    unittest.main()
