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
import socket
import subprocess
import time
from contextlib import contextmanager
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

# Substrings that strongly indicate the captured page is a bot/anti-scraping
# challenge (Cloudflare, Akamai, Imperva, PerimeterX, etc.) rather than the
# actual article. Matched case-insensitively against the first ~16KB.
BOT_CHALLENGE_MARKERS = (
    b'<title>just a moment',
    b'<title>attention required',
    b'<title>access denied',
    b'<title>verify you are human',
    b'<title>verifying you are human',
    b'cf-browser-verification',
    b'__cf_chl_',
    b'cf_chl_opt',
    b'pardon our interruption',
    b'_incapsula_resource',
    b'distil_r_blocked',
    b'px-captcha',
)

ARCHIVE_FOLDER_NAME = '_canvas_grab_archive'
SIDECAR_SUFFIX = '_external'


DEFAULT_CHROME_USER_DATA_DIR = '~/.canvas_grab/chrome-profile'

CHROME_EXECUTABLE_CANDIDATES = (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
)


class FetchExternalConfig(Configurable):
    """Persisted settings for external resource archiving.

    When ``enabled`` is true, a normal ``canvas_grab`` run performs the external
    fetch immediately after the Canvas sync completes. The ``--fetch-external``
    CLI flag is independent: it always runs the external fetch (and skips the
    Canvas sync), regardless of this setting.

    When ``with_chrome`` is true (or ``--with-chrome`` is passed on the CLI), the
    fetch spawns a real Chrome instance using ``chrome_user_data_dir`` and
    drives SingleFile against it via Chrome DevTools Protocol — which gets past
    most anti-bot challenges and lets you keep a persistent profile (with
    extensions like uBlock Origin, accepted cookie banners, logged-in sessions).
    """

    def __init__(self):
        self.enabled: bool = False
        self.exclude_domains: list[str] = []
        self.with_chrome: bool = False
        self.chrome_user_data_dir: str = DEFAULT_CHROME_USER_DATA_DIR
        self.chrome_executable: str = ''  # empty = auto-detect
        self.chrome_remote_port: int = 0  # 0 = pick a free port

    def to_config(self):
        return {
            'enabled': self.enabled,
            'exclude_domains': list(self.exclude_domains),
            'with_chrome': self.with_chrome,
            'chrome_user_data_dir': self.chrome_user_data_dir,
            'chrome_executable': self.chrome_executable,
            'chrome_remote_port': self.chrome_remote_port,
        }

    def from_config(self, config):
        self.enabled = bool(config.get('enabled', False))
        self.exclude_domains = list(config.get('exclude_domains', []) or [])
        self.with_chrome = bool(config.get('with_chrome', False))
        self.chrome_user_data_dir = (
            config.get('chrome_user_data_dir') or DEFAULT_CHROME_USER_DATA_DIR
        )
        self.chrome_executable = config.get('chrome_executable', '') or ''
        self.chrome_remote_port = int(config.get('chrome_remote_port', 0) or 0)


@dataclass
class FetchOptions:
    skip_existing: bool = True
    sub_langs: str = 'en.*,en'
    video_format: str = 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b'
    user_agent: Optional[str] = None
    singlefile_image: str = 'capsulecode/singlefile'
    singlefile_timeout: int = 180
    singlefile_extra_args: tuple = ()
    # When set, articles are fetched with the local `single-file` CLI talking
    # to a Chrome at this Chrome-DevTools-Protocol HTTP discovery URL
    # (e.g. "http://localhost:9222"). Set automatically by `run()` when
    # with_chrome is true; can also be pointed at an externally-managed browser.
    singlefile_browser_server: Optional[str] = None
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


def find_chrome_executable() -> Optional[str]:
    """Locate a Chrome/Chromium binary by checking known paths, then PATH."""
    for cand in CHROME_EXECUTABLE_CANDIDATES:
        if Path(cand).exists():
            return cand
    for cmd in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'):
        path = shutil.which(cmd)
        if path:
            return path
    return None


def _free_port(start: int = 9222) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('no free TCP port for Chrome remote debugging')


@contextmanager
def managed_chrome(executable: str, user_data_dir: str, port: int = 0,
                   startup_timeout: float = 20.0):
    """Spawn a Chrome with a persistent profile and remote debugging.

    Yields the HTTP CDP discovery URL (e.g. ``http://localhost:9222``) suitable
    for ``single-file --browser-server``. The Chrome process is terminated
    cleanly on exit (including KeyboardInterrupt), but the profile directory
    persists across runs so installed extensions, cookies, and accepted
    consent banners stick around.
    """
    profile = Path(user_data_dir).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    port = port or _free_port()
    cmd = [
        executable,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile}',
        '--no-first-run',
        '--no-default-browser-check',
        'about:blank',
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    discovery = f'http://localhost:{port}'
    try:
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            try:
                requests.get(f'{discovery}/json/version', timeout=1).raise_for_status()
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f'Chrome exited prematurely (code {proc.returncode})'
                    )
                time.sleep(0.4)
        else:
            raise RuntimeError(
                f'Chrome did not become reachable on {discovery} within '
                f'{startup_timeout:.0f}s'
            )
        yield discovery
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _looks_like_bot_challenge(head: bytes) -> bool:
    lo = head.lower()
    return any(m in lo for m in BOT_CHALLENGE_MARKERS)


def is_valid_html_archive(path: Path) -> bool:
    """True if `path` looks like a real archived page (and not a bot block).

    Used both right after fetching and when deciding whether to honor
    `skip_existing` on a previously-saved file, so already-archived bot
    challenges get re-attempted on the next run.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, 'rb') as f:
        head = f.read(16 * 1024)
    leading = head.lstrip().lower()
    if not leading.startswith(b'<!doctype html') and not leading.startswith(b'<html'):
        return False
    if _looks_like_bot_challenge(head):
        return False
    return True


def fetch_article(url: str, dest: Path, opts: FetchOptions) -> bool:
    """Archive `url` into `dest` using SingleFile.

    Two modes, selected by ``opts.singlefile_browser_server``:
    - Unset: ``docker run capsulecode/singlefile`` (writes HTML to stdout).
    - Set:   local ``single-file --browser-server <url>`` driving an external
             Chrome (writes HTML to a positional output file). This route gets
             past most anti-bot challenges and inherits the Chrome profile's
             extensions/cookies/sessions.

    Returns False (and removes any partial output) on subprocess error, timeout,
    non-HTML content, or a captured bot/anti-scraping challenge page.
    """
    use_local = bool(opts.singlefile_browser_server)
    if use_local:
        cmd = ['single-file', '--browser-server', opts.singlefile_browser_server]
    else:
        cmd = ['docker', 'run', '--rm', '-i', opts.singlefile_image]
    if opts.user_agent:
        cmd += ['--user-agent', opts.user_agent]
    cmd += list(opts.singlefile_extra_args)
    cmd.append(url)
    if use_local:
        cmd.append(str(dest))
    try:
        if use_local:
            proc = subprocess.run(
                cmd,
                stdout=None if opts.verbose else subprocess.DEVNULL,
                stderr=None if opts.verbose else subprocess.DEVNULL,
                check=False,
                timeout=opts.singlefile_timeout,
            )
        else:
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
    if not is_valid_html_archive(dest):
        with open(dest, 'rb') as f:
            head = f.read(2048).lower()
        dest.unlink()
        if _looks_like_bot_challenge(head):
            print(f'      {colored("blocked: anti-bot challenge (Cloudflare/etc.)", "yellow")}')
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


def _replace_iframe_with_video(soup: BeautifulSoup, iframe, src: str, original_src: str):
    video = soup.new_tag('video', controls='', src=src)
    # Preserve the original embed URL so a future run can recover if the local
    # file is lost (e.g. yt-dlp output deleted manually).
    video['data-original-src'] = original_src
    for k in ('width', 'height'):
        if iframe.has_attr(k):
            video[k] = iframe[k]
    iframe.replace_with(video)


def _recover_url(el, attr: str, sidecar: Path) -> Optional[str]:
    """If `el[attr]` is a local-relative path whose target is missing or invalid,
    return the original http(s) URL recorded in ``data-original-<attr>``.

    Returns None when nothing needs to be recovered (the local target is fine,
    or there's no recorded original to fall back to).
    """
    current = (el.get(attr) or '').strip()
    original = el.get(f'data-original-{attr}')
    if not original or not original.startswith(('http://', 'https://')):
        return None
    if not current or current.startswith(('http://', 'https://')):
        return None
    local = sidecar.parent / current
    if not local.exists():
        return original
    if local.suffix.lower() == '.html' and not is_valid_html_archive(local):
        try:
            local.unlink()
        except OSError:
            pass
        return original
    return None


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
            # Possibly a previous rewrite — try to recover the original URL.
            recovered = _recover_url(el, attr, sidecar)
            if not recovered:
                continue
            url = recovered
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
                _replace_iframe_with_video(soup, el, rel, url)
            else:
                el[f'data-original-{attr}'] = url
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

            el[f'data-original-{attr}'] = url
            el[attr] = f'{sidecar.name}/{fname}'
            rewrites += 1
            continue

        fname = article_filename(url)
        target = sidecar / fname

        if opts.skip_existing and is_valid_html_archive(target):
            skipped += 1
        else:
            # Drop a stale/invalid cached file (e.g. bot-blocked from a prior run)
            # before re-fetching so we don't carry a misleading archive forward.
            if target.exists():
                target.unlink()
            sidecar.mkdir(parents=True, exist_ok=True)
            print(f'    {colored("article", "cyan")} {url}')
            ok = fetch_article(url, target, opts)
            if not ok:
                failed += 1
                print(f'      {colored("failed", "red")}')
                # Restore the original URL on the link if it was previously
                # rewritten to point at a local copy that no longer exists.
                if (el.get(attr) or '').strip() and not (el.get(attr) or '').startswith(('http://', 'https://')):
                    el[attr] = url
                    if el.has_attr(f'data-original-{attr}'):
                        del el[f'data-original-{attr}']
                    rewrites += 1
                continue
            downloaded += 1

        el[f'data-original-{attr}'] = url
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


@dataclass
class ChromeOptions:
    """How to launch the Chrome instance for ``--with-chrome`` mode."""
    enabled: bool = False
    user_data_dir: str = DEFAULT_CHROME_USER_DATA_DIR
    executable: str = ''  # empty = auto-detect
    remote_port: int = 0  # 0 = pick a free port


def _process_html_files(html_files, base, opts):
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


def run(download_folder: str, opts: Optional[FetchOptions] = None,
        chrome: Optional[ChromeOptions] = None) -> None:
    opts = opts or FetchOptions()
    chrome = chrome or ChromeOptions()
    base = Path(download_folder)
    if not base.is_dir():
        print(colored(f'Download folder does not exist: {download_folder}', 'red'))
        return

    missing = []
    if not shutil.which('yt-dlp'):
        missing.append(('yt-dlp', 'brew install yt-dlp  # or: pipx install yt-dlp'))
    if chrome.enabled:
        # Local SingleFile CLI + a Chrome on disk.
        if not shutil.which('single-file'):
            missing.append((
                'single-file',
                'npm install -g single-file-cli  # used with --with-chrome',
            ))
        chrome_exe = chrome.executable or find_chrome_executable()
        if not chrome_exe or not Path(chrome_exe).exists():
            missing.append((
                'Google Chrome / Chromium',
                'install Chrome (or set [fetch_external].chrome_executable in config.toml)',
            ))
    else:
        # Docker SingleFile.
        if not shutil.which('docker'):
            missing.append(('docker', 'install Docker Desktop or `brew install --cask docker`'))
    if missing:
        for tool, install in missing:
            print(colored(f'{tool} is not installed.', 'red'))
            print(f'  Install with: {install}')
        return

    if not chrome.enabled:
        # Pull the SingleFile image up-front so the first article fetch isn't slow
        # and a missing/typo'd image fails fast instead of per-URL.
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

    if chrome.enabled:
        chrome_exe = chrome.executable or find_chrome_executable()
        profile = Path(chrome.user_data_dir).expanduser()
        print(colored(
            f'Launching Chrome with profile {profile} ...', 'cyan'))
        with managed_chrome(chrome_exe, str(profile), port=chrome.remote_port) as discovery:
            print(colored(f'Chrome ready at {discovery}', 'cyan'))
            opts.singlefile_browser_server = discovery
            try:
                _process_html_files(html_files, base, opts)
            finally:
                opts.singlefile_browser_server = None
    else:
        _process_html_files(html_files, base, opts)
