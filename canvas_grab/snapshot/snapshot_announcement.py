from dataclasses import dataclass, field
from html import escape
from datetime import datetime

from .snapshot_file import from_canvas_file
from ..utils import normalize_path, file_regex


@dataclass
class SnapshotAnnouncement:
    """Canvas announcement (a discussion topic with is_announcement=True)."""
    title: str
    message: str
    url: str = ''
    modified_at: int = 0

    posted_at: str = ''
    author: str = ''

    attachments: list = field(default_factory=list)

    def content(self):
        posted_str = ''
        if self.posted_at:
            try:
                dt = datetime.fromisoformat(self.posted_at.replace('Z', '+00:00'))
                posted_str = dt.strftime('%B %d, %Y at %I:%M %p')
            except Exception:
                posted_str = self.posted_at

        attachments_html = ''
        if self.attachments:
            from ..utils import normalize_path, file_regex
            normalized_title = normalize_path(self.title, file_regex)
            attachments_html = '<h2>Attachments</h2><ul>'
            for attachment in self.attachments:
                rel_path = f'./{escape(normalized_title)}_files/{escape(attachment.name)}'
                attachments_html += f'<li><a href="{rel_path}">{escape(attachment.name)}</a></li>'
            attachments_html += '</ul>'

        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8" />
    <title>{escape(self.title)}</title>
    <meta name="canvas-announcement-url" content="{escape(self.url)}" />
</head>
<body>
    <h1>{escape(self.title)}</h1>

    <div class="metadata">
        <p><strong>Posted:</strong> {escape(posted_str) or 'Unknown'}</p>
        <p><strong>Author:</strong> {escape(self.author) or 'Unknown'}</p>
        <p><strong>Canvas URL:</strong> <a href="{escape(self.url)}">{escape(self.url)}</a></p>
    </div>

    <hr />

    <div class="message">
        {self.message}
    </div>

    {attachments_html}
</body>
</html>'''


def _ts_from_iso(iso: str) -> int:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except Exception:
        return 0


def _date_prefix(iso: str) -> str:
    if not iso:
        return ''
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return ''


def announcement_filename(topic) -> str:
    """Return the relative path used to store an announcement on disk.

    Format: ``Announcements/<YYYY-MM-DD>_<slug>.html``. The date prefix makes
    chronological listing trivial; we fall back to ``updated_at`` and finally
    just the slug if neither timestamp is present.
    """
    title = getattr(topic, 'title', None) or 'untitled'
    slug = normalize_path(title, file_regex)
    date = _date_prefix(getattr(topic, 'posted_at', '') or '') \
        or _date_prefix(getattr(topic, 'updated_at', '') or '')
    name = f'{date}_{slug}' if date else slug
    return f'Announcements/{name}.html'


def from_canvas_topic(topic):
    """Build a :class:`SnapshotAnnouncement` from a canvasapi discussion topic.

    Returns ``(key, snapshot, attachments)`` where ``attachments`` is a list of
    ``(attachment_key, SnapshotFile)`` pairs the caller should also add to the
    snapshot so they get downloaded alongside the announcement HTML.
    """
    title = getattr(topic, 'title', None) or 'untitled'
    message = getattr(topic, 'message', '') or ''
    posted_at = getattr(topic, 'posted_at', '') or ''
    updated_at = getattr(topic, 'updated_at', '') or ''

    author_obj = getattr(topic, 'author', {}) or {}
    if isinstance(author_obj, dict):
        author = author_obj.get('display_name') or author_obj.get('name') or ''
    else:
        author = getattr(author_obj, 'display_name', '') or getattr(author_obj, 'name', '') or ''

    modified_at = _ts_from_iso(updated_at) or _ts_from_iso(posted_at)

    attachment_files = []
    attachments_pairs = []
    raw_attachments = getattr(topic, 'attachments', None) or []
    slug = normalize_path(title, file_regex)
    for attach in raw_attachments:
        try:
            snapshot_file = from_canvas_file(attach)
        except Exception:
            continue
        attachment_files.append(snapshot_file)
        attach_key = (
            f'Announcements/{slug}_files/'
            f'{normalize_path(snapshot_file.name, file_regex)}'
        )
        attachments_pairs.append((attach_key, snapshot_file))

    snapshot = SnapshotAnnouncement(
        title=title,
        message=message,
        url=getattr(topic, 'html_url', '') or '',
        modified_at=modified_at,
        posted_at=posted_at,
        author=author,
        attachments=attachment_files,
    )
    return announcement_filename(topic), snapshot, attachments_pairs
