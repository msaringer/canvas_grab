from dataclasses import dataclass
from html import escape


@dataclass
class SnapshotPage:
    """Canvas page with actual HTML content."""
    title: str
    body: str
    url: str = ''
    modified_at: int = 0

    def content(self):
        """Generate complete HTML document."""
        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8" />
    <title>{escape(self.title)}</title>
    <meta name="canvas-page-url" content="{escape(self.url)}" />
</head>
<body>
{self.body}
</body>
</html>'''
