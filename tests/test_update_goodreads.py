import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import update_goodreads as app

FEED = b'''<rss><channel><item><title>A [book]</title>
<author_name>Author</author_name><link>https://www.goodreads.com/book/show/1</link>
<average_rating>4.1</average_rating></item></channel></rss>'''


class ReadingListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'README.md'
        self.original = f'Before\n{app.START}\nold book\n{app.END}\nAfter\n'
        self.path.write_text(self.original, encoding='utf-8')

    def test_success_changes_only_section_and_second_run_is_unchanged(self):
        with patch.object(app, 'fetch', return_value=FEED):
            self.assertEqual(app.update(self.path, '200390160'), 'updated')
            content = self.path.read_text()
            self.assertTrue(content.startswith('Before\n' + app.START))
            self.assertTrue(content.endswith(app.END + '\nAfter\n'))
            self.assertIn(r'A \[book\]', content)
            self.assertEqual(app.update(self.path, '200390160'), 'unchanged')

    def test_source_failure_preserves_bytes(self):
        with patch.object(app, 'fetch', side_effect=app.Unavailable('HTTP 404')):
            with self.assertRaises(app.Unavailable):
                app.update(self.path, '200390160')
        self.assertEqual(self.path.read_bytes(), self.original.encode())

    def test_bad_responses_never_overwrite_snapshot(self):
        for payload in (b'broken', b'<html/>', FEED.replace(b'https:', b'javascript:')):
            with self.subTest(payload=payload), patch.object(app, 'fetch', return_value=payload):
                with self.assertRaises((ValueError, app.ET.ParseError)):
                    app.update(self.path, '200390160')
                self.assertEqual(self.path.read_bytes(), self.original.encode())

    def test_valid_empty_feed_clears_old_books(self):
        with patch.object(app, 'fetch', return_value=b'<rss><channel/></rss>'):
            self.assertEqual(app.update(self.path, '200390160'), 'updated')
        self.assertNotIn('old book', self.path.read_text())

    def test_bad_markers_fail_before_network(self):
        for content in ('missing', app.END + app.START, self.original + app.START):
            self.path.write_text(content)
            with patch.object(app, 'fetch') as fetch:
                with self.assertRaises(ValueError):
                    app.update(self.path, '200390160')
                fetch.assert_not_called()

    def test_404_is_not_retried(self):
        with patch.object(app, 'urlopen', side_effect=HTTPError('url', 404, 'missing', {}, None)) as fetch:
            with self.assertRaises(app.Unavailable):
                app.fetch('200390160')
            self.assertEqual(fetch.call_count, 1)

    def test_temporary_errors_retry_then_stop(self):
        for error in (URLError('offline'), HTTPError('url', 503, 'busy', {}, None)):
            with patch.object(app, 'urlopen', side_effect=error) as fetch, patch.object(app.time, 'sleep'):
                with self.assertRaises(app.Unavailable):
                    app.fetch('200390160')
                self.assertEqual(fetch.call_count, 3)

    def test_auth_error_remains_a_failure(self):
        with patch.object(app, 'urlopen', side_effect=HTTPError('url', 403, 'denied', {}, None)):
            with self.assertRaises(ValueError):
                app.fetch('200390160')

    def test_unavailable_is_reported_separately_from_success(self):
        output = Path(self.tmp.name) / 'output'
        summary = Path(self.tmp.name) / 'summary'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}):
            with patch.object(app, 'update', side_effect=app.Unavailable('HTTP 404')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(app.main(), 0)
        self.assertEqual(output.read_text(), 'status=unavailable\n')
        self.assertIn('No fresh data was obtained', summary.read_text())

    def test_code_error_fails_job(self):
        with patch.object(app, 'update', side_effect=ValueError('bad markers')):
            with patch.dict(os.environ, {'GITHUB_OUTPUT': '', 'GITHUB_STEP_SUMMARY': ''}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(app.main(), 1)


if __name__ == '__main__':
    unittest.main()
