from dataclasses import dataclass, field
from html import escape
from datetime import datetime


@dataclass
class SnapshotAssignment:
    """Canvas assignment with HTML description and metadata."""
    title: str
    description: str
    url: str = ''
    modified_at: int = 0

    # Metadata
    due_at: str = ''
    points_possible: float = 0.0
    submission_types: list = field(default_factory=list)

    # Attachments
    attachments: list = field(default_factory=list)

    def content(self):
        """Generate complete HTML document."""
        # Format due date
        due_date_str = 'No due date'
        if self.due_at:
            try:
                dt = datetime.fromisoformat(self.due_at.replace('Z', '+00:00'))
                due_date_str = dt.strftime('%B %d, %Y at %I:%M %p')
            except:
                due_date_str = self.due_at

        # Format points
        points_str = f'{self.points_possible} points' if self.points_possible else 'Ungraded'

        # Format submission types
        submission_str = ', '.join(self.submission_types) if self.submission_types else 'None'

        # Build attachments section
        attachments_html = ''
        if self.attachments:
            from ..utils import normalize_path, file_regex
            normalized_title = normalize_path(self.title, file_regex)
            attachments_html = '<h2>Attachments</h2><ul>'
            for attachment in self.attachments:
                # Relative path to attachment
                rel_path = f'./{escape(normalized_title)}_files/{escape(attachment.name)}'
                attachments_html += f'<li><a href="{rel_path}">{escape(attachment.name)}</a></li>'
            attachments_html += '</ul>'

        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8" />
    <title>{escape(self.title)}</title>
    <meta name="canvas-assignment-url" content="{escape(self.url)}" />
</head>
<body>
    <h1>{escape(self.title)}</h1>

    <div class="metadata">
        <p><strong>Due:</strong> {escape(due_date_str)}</p>
        <p><strong>Points:</strong> {escape(points_str)}</p>
        <p><strong>Submission Types:</strong> {escape(submission_str)}</p>
        <p><strong>Canvas URL:</strong> <a href="{escape(self.url)}">{escape(self.url)}</a></p>
    </div>

    <hr />

    <div class="description">
        {self.description}
    </div>

    {attachments_html}
</body>
</html>'''
