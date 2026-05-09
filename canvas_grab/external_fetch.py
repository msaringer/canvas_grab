"""Fetch external resources (web pages, videos) referenced by downloaded Canvas HTML pages.

For each ``*.html`` file under the download folder, this module:
- parses the HTML for external links and iframe embeds
- downloads articles via the ``capsulecode/singlefile`` Docker image (a real headless
  Chrome — produces dramatically smaller archives than CLI tools that use a static
  fetcher), videos via ``yt-dlp``, and PDFs/binaries directly via ``requests``
- writes everything into a sidecar folder named ``<page>_external/`` next to the
  original HTML
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

import requests
from bs4 import BeautifulSoup
from termcolor import colored

from .configurable import Configurable


VIDEO_HOSTS = (
    'youtube.com', 'm.youtube.com', 'youtube-nocookie.com',
    'youtu.be',
    'vimeo.com', 'player.vimeo.com',
    'dailymotion.com',
)

CANVAS_HOSTS = ('canvas.sfu.ca',)

# Skip these — interactive/non-archivable embeds.
SKIP_HOSTS = ('h5p.org',)

# URL paths ending in these extensions are downloaded raw (with `requests`)
# instead of through SingleFile, which would wrap them in HTML.
BINARY_EXTS = (
    '.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
    '.zip', '.epub', '.mp3', '.csv', '.rtf',
)

ARCHIVE_FOLDER_NAME = '_canvas_grab_archive'
SIDECAR_SUFFIX = '_external'


class FetchExternalConfig(Configurable):
    """Persisted settings for external resource archiving.

    When ``enabled`` is true, a normal ``canvas_grab`` run performs the external
    fetch immediately after the Canvas sync completes. The ``--fetch-external``
    CLI flag is independent: it always runs the external fetch (and skips the
    Canvas sync), regardless of this setting.
    """

    def __init__(self):
        self.enabled: bool = False
        self.exclude_domains: list[str] = []

    def to_config(self):
        return {
            'enabled': self.enabled,
            'exclude_domains': list(self.exclude_domains),
        }

    def from_config(self, config):
        self.enabled = bool(config.get('enabled', False))
        self.exclude_domains = list(config.get('exclude_domains', []) or [])


@dataclass
class FetchOptions:
    skip_existing: bool = True
    sub_langs: str = 'en.*,en'
    video_format: str = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b'
    user_agent: Optional[str] = None
    singlefile_image: str = 'capsulecode/singlefile'
    singlefile_timeout: int = 180
    singlefile_extra_args: tuple = ()
    yt_dlp_extra_args: tuple = ()
    verbose: bool = False
    # Extra domains to skip in addition to CANVAS_HOSTS / SKIP_HOSTS. Matched by
    # exact host or subdomain (e.g. "sfu.ca" matches "www.sfu.ca" and "x.y.sfu.ca").
    exclude_domains: tuple = ()


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


def url_binary_ext(url: str) -> Optional[str]:
    """If the URL path ends in a known binary extension, return it (with the dot)."""
    path = urlparse(url).path.lower()
    for ext in BINARY_EXTS:
        if path.endswith(ext):
            return ext
    return None


def _composed_filename(url: str, ext: str) -> str:
    p = urlparse(url)
    host = (p.hostname or 'site').lower()
    if host.startswith('www.'):
        host = host[4:]
    segs = [s for s in p.path.split('/') if s]
    last = segs[-1] if segs else 'index'
    if last.lower().endswith(ext):
        last = last[: -len(ext)]
    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()[:6]
    return f'{_slug(host, 40)}__{_slug(last, 60)}__{digest}{ext}'


def article_filename(url: str) -> str:
    return _composed_filename(url, '.html')


def binary_filename(url: str, ext: str) -> str:
    return _composed_filename(url, ext)


def video_basename(url: str) -> str:
    """Stable prefix for the video file. The actual filename adds the title at
    download time (yt-dlp's ``%(title)s``)."""
    host = (_host(url) or 'video').replace('www.', '')
    short = host.split('.')[0] if '.' in host else host
    return f'{_slug(short, 20)}_{_predicted_id(url)}'


def find_existing_video(dest_dir: Path, basename: str) -> Optional[Path]:
    """Locate a previously-downloaded video by its stable prefix.

    Tolerates both new (``<basename>_<title>.mp4``) and old (``<basename>.mp4``)
    naming so a re-run after the title-in-filename change doesn't re-download.
    """
    if not dest_dir.is_dir():
        return None
    exact = dest_dir / f'{basename}.mp4'
    if exact.exists():
        return exact
    for p in sorted(dest_dir.glob(f'{basename}_*.mp4')):
        return p
    return None


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
    """Archive `url` into `dest` using SingleFile via Docker.

    SingleFile uses a real headless Chrome inside the container, so it captures
    only the rendered DOM (no lazy-loaded image alternates, no analytics blobs,
    no oversized inlined videos). HTML is written to stdout by the container,
    which we stream straight into `dest`.
    """
    cmd = ['docker', 'run', '--rm', '-i', opts.singlefile_image]
    if opts.user_agent:
        cmd += ['--user-agent', opts.user_agent]
    cmd += list(opts.singlefile_extra_args)
    cmd.append(url)
    try:
        with open(dest, 'wb') as f:
            proc = subprocess.run(
                cmd,
                stdout=f,
                stderr=None if opts.verbose else subprocess.DEVNULL,
                check=False,
                timeout=opts.singlefile_timeout,
            )
    except subprocess.TimeoutExpired:
        if dest.exists():
            dest.unlink()
        return False
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        if dest.exists():
            dest.unlink()
        return False
    # Sanity check: SingleFile output starts with an HTML doctype.
    with open(dest, 'rb') as f:
        head = f.read(64).lstrip().lower()
    if not head.startswith(b'<!doctype html') and not head.startswith(b'<html'):
        dest.unlink()
        return False
    return True


def fetch_video(url: str, dest_dir: Path, basename: str, opts: FetchOptions) -> Optional[Path]:
    # `%(title).80B` caps the title at 80 bytes so the resulting filename
    # stays well under typical filesystem limits.
    template = str(dest_dir / f'{basename}_%(title).80B.%(ext)s')
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
    return find_existing_video(dest_dir, basename)


def fetch_binary(url: str, dest: Path, opts: FetchOptions) -> bool:
    headers = {'User-Agent': opts.user_agent or 'Mozilla/5.0 (canvas_grab)'}
    try:
        with requests.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
    except Exception:
        if dest.exists():
            dest.unlink()
        return False
    return dest.exists() and dest.stat().st_size > 0


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
        if opts.exclude_domains and _host_matches(_host(url), opts.exclude_domains):
            continue

        if is_video_url(url):
            dl_url = normalize_video_url(url)
            base = video_basename(dl_url)
            existing = find_existing_video(sidecar, base)

            if opts.skip_existing and existing is not None:
                target = existing
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
            continue

        bin_ext = url_binary_ext(url)
        if bin_ext:
            fname = binary_filename(url, bin_ext)
            target = sidecar / fname

            if opts.skip_existing and target.exists():
                skipped += 1
            else:
                sidecar.mkdir(parents=True, exist_ok=True)
                print(f'    {colored(bin_ext.lstrip(".").ljust(7), "cyan")} {url}')
                if not fetch_binary(url, target, opts):
                    failed += 1
                    print(f'      {colored("failed", "red")}')
                    continue
                downloaded += 1

            el[attr] = f'{sidecar.name}/{fname}'
            rewrites += 1
            continue

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
    if not shutil.which('docker'):
        missing.append(('docker', 'install Docker Desktop or `brew install --cask docker`'))
    if not shutil.which('yt-dlp'):
        missing.append(('yt-dlp', 'brew install yt-dlp  # or: pipx install yt-dlp'))
    if missing:
        for tool, install in missing:
            print(colored(f'{tool} is not installed.', 'red'))
            print(f'  Install with: {install}')
        return
    # Pull the SingleFile image up-front so the first article fetch isn't slow
    # and so a missing/typo'd image fails fast instead of per-URL.
    pull = subprocess.run(
        ['docker', 'image', 'inspect', opts.singlefile_image],
        capture_output=True, check=False,
    )
    if pull.returncode != 0:
        print(colored(f'Pulling Docker image {opts.singlefile_image}...', 'cyan'))
        proc = subprocess.run(
            ['docker', 'pull', opts.singlefile_image], check=False,
        )
        if proc.returncode != 0:
            print(colored(
                f'Failed to pull {opts.singlefile_image}. '
                'Is the Docker daemon running?', 'red'))
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
