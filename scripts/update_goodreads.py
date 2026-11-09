"""Refresh the optional Goodreads snapshot, reporting source outages explicitly."""
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

START = '<!-- GOODREADS-LIST:START -->'
END = '<!-- GOODREADS-LIST:END -->'
LIMIT = 2 * 1024 * 1024


class Unavailable(Exception):
    """The external source cannot currently supply the snapshot."""


def fetch(user_id):
    request = Request(
        f'https://www.goodreads.com/review/list_rss/{user_id}?shelf=currently-reading',
        headers={'User-Agent': 'Mozilla/5.0'})
    for attempt in range(3):
        delay = 2 ** attempt
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read(LIMIT + 1)
            if len(payload) > LIMIT:
                raise ValueError('RSS exceeds the 2 MiB size limit.')
            return payload
        except HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
            exc.close()
            if code in {404, 410}:
                raise Unavailable(f'Goodreads RSS returned HTTP {code}.') from exc
            if code not in {429, 500, 502, 503, 504}:
                raise ValueError(f'Unexpected Goodreads HTTP status: {code}.') from exc
            reason = f'HTTP {code}'
            if retry_after.isdigit():
                delay = min(30, max(delay, int(retry_after)))
        except (URLError, TimeoutError) as exc:
            reason = type(exc).__name__
        if attempt < 2:
            time.sleep(delay)
    raise Unavailable(f'Goodreads RSS unavailable after 3 attempts ({reason}).')


def escape(value):
    value = ' '.join(value.split())
    value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*\[\]_])', r'\\\1', value)


def render(payload):
    root = ET.fromstring(payload)
    channel = root.find('channel')
    if root.tag != 'rss' or channel is None:
        raise ValueError('Response is not an RSS feed.')
    lines = []
    for item in channel.findall('item')[:10]:
        title, author, link, rating = (
            (item.findtext(tag) or '').strip()
            for tag in ('title', 'author_name', 'link', 'average_rating'))
        url = urlsplit(link)
        if (not title or not author or url.scheme != 'https'
                or url.hostname not in {'goodreads.com', 'www.goodreads.com'}
                or url.username or url.password or url.port not in {None, 443}):
            raise ValueError('RSS contains an invalid book entry.')
        link = quote(link, safe=':/?&=%#@+,-._~')
        lines.append(f'- [{escape(title)}]({link}) by {escape(author)} (⭐️{escape(rating) or "N/A"})')
    return '\n'.join(lines)


def update(path, user_id):
    if not re.fullmatch(r'[0-9]+', user_id):
        raise ValueError('GOODREADS_USER_ID must be numeric.')
    original = path.read_bytes().decode('utf-8')
    if original.count(START) != 1 or original.count(END) != 1:
        raise ValueError('README requires exactly one Goodreads START/END pair.')
    left, right = original.index(START) + len(START), original.index(END)
    if left > right:
        raise ValueError('Goodreads README markers are reversed.')
    body = render(fetch(user_id))
    updated = original[:left] + '\n' + body + '\n' + original[right:]
    if updated == original:
        return 'unchanged'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(updated.encode('utf-8'))
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 'updated'


def report(status, message):
    print(message)
    for variable, content in (
        ('GITHUB_OUTPUT', f'status={status}\n'),
        ('GITHUB_STEP_SUMMARY', f'### Goodreads: {status}\n\n{message}\n'),
    ):
        if os.environ.get(variable):
            with open(os.environ[variable], 'a', encoding='utf-8') as stream:
                stream.write(content)


def main():
    try:
        status = update(Path('README.md'), os.environ.get('GOODREADS_USER_ID', ''))
    except Unavailable as exc:
        message = (f'{exc} Refresh skipped; the existing list is preserved and may be stale. '
                   'No fresh data was obtained. Check feed availability, account ID and visibility.')
        report('unavailable', message)
        print('::warning title=Goodreads refresh skipped::' + message)
        return 0  # Optional source outage, not a successful sync.
    except (ValueError, ET.ParseError, OSError, UnicodeError) as exc:
        report('failed', f'Goodreads refresh failed: {exc}')
        return 1
    report(status, 'Feed fetched and validated. ' + (
        'README updated.' if status == 'updated' else 'Reading list already matches the feed.'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
