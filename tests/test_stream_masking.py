"""Raw process output must remain masked after the private-key hold buffer fills."""
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.agents' / 'bin'))
from teamlib import run


class StreamMaskingTest(unittest.TestCase):
    def test_long_raw_private_keys_hide_every_body_line(self):
        header = '-----BEGIN RSA ' + 'PRIVATE KEY-----'
        for count in (run.HOLD_LINES - 2, run.HOLD_LINES - 1,
                      run.HOLD_LINES, run.HOLD_LINES + 10, run.HOLD_LINES * 3):
            for closed in (False, True):
                with self.subTest(count=count, closed=closed):
                    body = ['SYNTHETICBODY%04d' % i + 'Ab0+' * 5 for i in range(count)]
                    lines = ['before', header, *body]
                    if closed:
                        lines += [header.replace('BEGIN', 'END'), 'after']
                    output = io.StringIO()
                    # Feed unsanitized bytes directly, without brief staging or sink preprocessing.
                    run.mask_stream(((line + '\n').encode() for line in lines), output)
                    result = output.getvalue()
                    for line in [header, *body]:
                        self.assertNotIn(line, result)
                    self.assertTrue(result.startswith('before\n***\n'))
                    if closed:
                        self.assertNotIn('PRIVATE KEY', result)
                        self.assertTrue(result.endswith('after\n'))

    def test_long_block_resumes_masking_after_end(self):
        header = '-----BEGIN EC ' + 'PRIVATE KEY-----'
        body = ['SYNTHETIC%04d' % i + 'Ab0+' * 5 for i in range(run.HOLD_LINES + 20)]
        output = io.StringIO()
        lines = [header, *body, header.replace('BEGIN', 'END'), 'between',
                 header, *body, header.replace('BEGIN', 'END'), 'after']
        run.mask_stream(((line + '\n').encode() for line in lines), output)
        self.assertEqual(output.getvalue(), '***\nbetween\n***\nafter\n')

    def test_overflow_discards_all_tail_lines_through_end(self):
        header = '-----BEGIN RSA ' + 'PRIVATE KEY-----'
        body = ['SYNTHETICBODY%04d' % i + 'Ab0+' * 5
                for i in range(run.HOLD_LINES + 10)]
        tail = ['synthetic tail with spaces', '', 'SYNTHETICTAIL=']
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=newline):
                output = io.StringIO()
                lines = [header, *body, *tail, header.replace('BEGIN', 'END'), 'after']
                run.mask_stream(((line + newline).encode() for line in lines), output)
                self.assertEqual(output.getvalue(), '***' + newline + 'after' + newline)

    def test_overflow_with_trailing_spaces_masks_held_and_tail_lines(self):
        header = '-----BEGIN RSA ' + 'PRIVATE KEY-----'
        for newline in ('\n', '\r\n'):
            for spaced in (5, run.HOLD_LINES - 2, run.HOLD_LINES + 5):
                for closed in (False, True):
                    with self.subTest(newline=newline, spaced=spaced, closed=closed):
                        body = ['SYNTHETICBODY%04d' % i + 'Ab0+' * 5
                                for i in range(450)]
                        body[spaced] += ' '
                        lines = ['before ' + header, *body]
                        if closed:
                            lines += [header.replace('BEGIN', 'END'), 'after']
                        output = io.StringIO()
                        run.mask_stream(((line + newline).encode() for line in lines), output)
                        result = output.getvalue()
                        for line in [header, *body]:
                            self.assertNotIn(line.rstrip(), result)
                        expected = 'before ***' + newline
                        if closed:
                            expected += 'after' + newline
                        self.assertEqual(result, expected)
