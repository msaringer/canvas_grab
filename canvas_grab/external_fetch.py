"""Fetch external resources (web pages, videos) referenced by downloaded Canvas HTML pages.

For each ``*.html`` file under the download folder, this module:
- parses the HTML for external links and iframe embeds
- downloads articles via ``monolith`` and videos via ``yt-dlp`` into a sidecar folder
  named ``<page>_external/`` next to the original HTML
- rewrites the original HTML to point to the local copies (preserving on-disk mtime
  so canvas_grab's planner won't think the page changed)

Re-running is idempotent: existing sidecar files are skipped and only the rewrite step
re-runs. Canvas-internal links (canvas.sfu.ca, ``instructure_file_link``) are left alone
because they're handled by the existing file-sync.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
from termcolor import colored


VIDEO_HOSTS = (
    'youtube.com', 'm.youtube.com', 'youtube-nocookie.com',
    'youtu.be',
    'vimeo.com', 'player.vimeo.com',
    'dailymotion.com',
)

CANVAS_HOSTS = ('canvas.sfu.ca',)

# Skip these — interactive/non-archivable embeds.
SKIP_HOSTS = ('h5p.org',)

ARCHIVE_FOLDER_NAME = '_canvas_grab_archive'
SIDECAR_SUFFIX = '_external'


@dataclass
class FetchOptions:
    skip_existing: bool = True
    sub_langs: str = 'en.*,en'
    video_format: str = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b'
    user_agent: Optional[str] = None
    monolith_extra_args: tuple = ()
    yt_dlp_extra_args: tuple = ()
    verbose: bool = False


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or '').lower()
    except Exception:
        return ''


def _host_matches(host: str, domains) -> bool:
    return any(host == d or host.endswith('.' + d) for d in domains)


def is_canvas_internal(url: str) -> bool:
    return _host_matches(_host(url), CANVAS_HOSTS)


def is_skip(url: str) -> bool:
    return _host_matches(_host(url), SKIP_HOSTS)


def is_video_url(url: str) -> bool:
    return _host_matches(_host(url), VIDEO_HOSTS)


def youtube_id(url: str) -> Optional[str]:
    host = _host(url)
    p = urlparse(url)
    if host.endswith('youtu.be'):
        seg = p.path.lstrip('/').split('/')[0]
        return seg or None
    if 'youtube' in host:
        if p.path.startswith('/embed/'):
            parts = p.path.split('/')
            return parts[2] if len(parts) >= 3 and parts[2] else None
        if p.path.startswith('/watch'):
            return (parse_qs(p.query).get('v') or [None])[0]
    return None


def vimeo_id(url: str) -> Optional[str]:
    if 'vimeo' not in _host(url):
        return None
    m = re.match(r'^/(?:video/)?(\d+)', urlparse(url).path)
    return m.group(1) if m else None


def normalize_video_url(url: str) -> str:
    """Turn an embed URL into its canonical watch URL (yt-dlp prefers these)."""
    yid = youtube_id(url)
    if yid:
        return f'https://www.youtube.com/watch?v={yid}'
    vid = vimeo_id(url)
    if vid:
        return f'https://vimeo.com/{vid}'
    return url


def _predicted_id(url: str) -> str:
    """A deterministic ID for the URL so we can predict yt-dlp's output filename."""
    return (
        youtube_id(url)
        or vimeo_id(url)
        or hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]
    )


_SAFE = re.compile(r'[^A-Za-z0-9._-]+')


def _slug(text: str, max_len: int = 80) -> str:
    s = _SAFE.sub('_', text).strip('_')
    return s[:max_len].strip('_') or 'untitled'


def article_filename(url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or 'site').lower()
    if host.startswith('www.'):
        host = host[4:]
    segs = [s for s in p.path.split('/') if s]
    last = segs[-1] if segs else 'index'
    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()[:6]
    return f'{_slug(host, 40)}__{_slug(last, 60)}__{digest}.html'


def video_basename(url: str) -> str:
    host = (_host(url) or 'video').replace('www.', '')
    short = host.split('.')[0] if '.' in host else host
    return f'{_slug(short, 20)}_{_predicted_id(url)}'


def collect_links(soup: BeautifulSoup) -> list[tuple]:
    """Return ``(element, attr, url)`` tuples for every <a href> and <iframe src>."""
    out = []
    for a in soup.find_all('a', href=True):
        if 'instructure_file_link' in (a.get('class') or []):
            continue
        out.append((a, 'href', a['href']))
    for f in soup.find_all('iframe', src=True):
        out.append((f, 'src', f['src']))
    return out


def fetch_article(url: str, dest: Path, opts: FetchOptions) -> bool:
    cmd = ['monolith', url, '-o', str(dest)]
    if opts.user_agent:
        cmd += ['--user-agent', opts.user_agent]
    cmd += list(opts.monolith_extra_args)
    proc = subprocess.run(
        cmd, capture_output=not opts.verbose, text=True, check=False
    )
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def fetch_video(url: str, dest_dir: Path, basename: str, opts: FetchOptions) -> Optional[Path]:
    template = str(dest_dir / f'{basename}.%(ext)s')
    cmd = [
        'yt-dlp',
        '--no-progress', '--no-warnings',
        '-f', opts.video_format,
        '--merge-output-format', 'mp4',
        '--write-subs', '--write-auto-subs',
        '--sub-langs', opts.sub_langs,
        '--sub-format', 'vtt/best',
        '--convert-subs', 'vtt',
        '--embed-subs',
        '-o', template,
        url,
    ]
    cmd += list(opts.yt_dlp_extra_args)
    proc = subprocess.run(
        cmd, capture_output=not opts.verbose, text=True, check=False
    )
    if proc.returncode != 0:
        return None
    out = dest_dir / f'{basename}.mp4'
    return out if out.exists() else None


def _replace_iframe_with_video(soup: BeautifulSoup, iframe, src: str):
    video = soup.new_tag('video', controls='', src=src)
    for k in ('width', 'height'):
        if iframe.has_attr(k):
            video[k] = iframe[k]
    iframe.replace_with(video)


def process_html_file(html_path: Path, opts: FetchOptions) -> tuple[int, int, int]:
    """Process one HTML file. Returns ``(downloaded, skipped, failed)``."""
    text = html_path.read_text(encoding='utf-8', errors='replace')
    soup = BeautifulSoup(text, 'html.parser')
    sidecar = html_path.with_name(html_path.stem + SIDECAR_SUFFIX)

    downloaded = skipped = failed = 0
    rewrites = 0

    for el, attr, original in collect_links(soup):
        url = original.strip()
        if url.startswith('//'):
            url = 'https:' + url
        if not url.startswith(('http://', 'https://')):
            continue
        if is_canvas_internal(url) or is_skip(url):
            continue

        if is_video_url(url):
            dl_url = normalize_video_url(url)
            base = video_basename(dl_url)
            target = sidecar / f'{base}.mp4'

            if opts.skip_existing and target.exists():
                skipped += 1
            else:
                sidecar.mkdir(parents=True, exist_ok=True)
                print(f'    {colored("video  ", "cyan")} {dl_url}')
                result = fetch_video(dl_url, sidecar, base, opts)
                if result is None:
                    failed += 1
                    print(f'      {colored("failed", "red")}')
                    continue
                target = result
                downloaded += 1

            rel = f'{sidecar.name}/{target.name}'
            if el.name == 'iframe':
                _replace_iframe_with_video(soup, el, rel)
            else:
                el[attr] = rel
            rewrites += 1
        else:
            fname = article_filename(url)
            target = sidecar / fname

            if opts.skip_existing and target.exists():
                skipped += 1
            else:
                sidecar.mkdir(parents=True, exist_ok=True)
                print(f'    {colored("article", "cyan")} {url}')
                ok = fetch_article(url, target, opts)
                if not ok:
                    failed += 1
                    if target.exists():
                        target.unlink()
                    print(f'      {colored("failed", "red")}')
                    continue
                downloaded += 1

            el[attr] = f'{sidecar.name}/{fname}'
            rewrites += 1

    if rewrites:
        st = html_path.stat()
        html_path.write_text(str(soup), encoding='utf-8')
        # Preserve mtime so canvas_grab's planner doesn't see the rewritten page
        # as drift from the Canvas snapshot.
        os.utime(html_path, (st.st_atime, st.st_mtime))

    return (downloaded, skipped, failed)


def _is_inside_sidecar(path: Path) -> bool:
    return any(part.endswith(SIDECAR_SUFFIX) for part in path.parent.parts)


def run(download_folder: str, opts: Optional[FetchOptions] = None) -> None:
    opts = opts or FetchOptions()
    base = Path(download_folder)
    if not base.is_dir():
        print(colored(f'Download folder does not exist: {download_folder}', 'red'))
        return

    missing = []
    if not shutil.which('monolith'):
        missing.append(('monolith', 'brew install monolith'))
    if not shutil.which('yt-dlp'):
        missing.append(('yt-dlp', 'brew install yt-dlp  # or: pipx install yt-dlp'))
    if missing:
        for tool, install in missing:
            print(colored(f'{tool} is not installed.', 'red'))
            print(f'  Install with: {install}')
        return

    html_files = sorted(
        p for p in base.rglob('*.html')
        if ARCHIVE_FOLDER_NAME not in p.parts and not _is_inside_sidecar(p)
    )
    print(f'Scanning {colored(str(len(html_files)), "cyan")} HTML files in {colored(str(base), "cyan")}')

    total_dl = total_skip = total_fail = 0
    for idx, html_path in enumerate(html_files, 1):
        rel = html_path.relative_to(base)
        print(f'({idx}/{len(html_files)}) {colored(str(rel), "cyan")}')
        try:
            d, s, f = process_html_file(html_path, opts)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f'  {colored(f"error: {e}", "red")}')
            continue
        total_dl += d
        total_skip += s
        total_fail += f

    print()
    print(
        f'Done. {colored(str(total_dl), "green")} downloaded, '
        f'{colored(str(total_skip), "yellow")} skipped (already present), '
        f'{colored(str(total_fail), "red")} failed.'
    )
