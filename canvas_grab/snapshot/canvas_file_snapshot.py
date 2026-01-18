from termcolor import colored

from .snapshot import Snapshot
from .snapshot_file import from_canvas_file
from .snapshot_link import SnapshotLink
from .snapshot_page import SnapshotPage
from .snapshot_assignment import SnapshotAssignment
from ..request_batcher import RequestBatcher
from canvasapi.exceptions import ResourceDoesNotExist
from ..utils import normalize_path, file_regex
from datetime import datetime

class CanvasFileSnapshot(Snapshot):
    """Takes a snapshot of files on Canvas, organized by file tab.

    ``CanvasFileSnapshot`` generates a snapshot of files on Canvas. In this snapshot mode,
    all files under "File" tab will be scanned as-is. Besides, it will add pages into
    the snapshot at `pages/xxx` path, if `with_link` option is enabled.
    """

    def __init__(self, course, with_link=False):
        """Create a file-based Canvas snapshot-taker

        Args:
            course (canvasapi.course.Course): The course object
            with_link (bool, optional): If true, pages will be included in snapshot. Defaults to False.
        """
        self.course = course
        self.with_link = with_link
        self.snapshot = {}

    def add_to_snapshot(self, key, value):
        """Add a key-value pair into snapshot. If duplicated, this function will report error and ignore the pair.

        Args:
            key (str): key or path of the object
            value (any): content of the object
        """
        if key in self.snapshot:
            print(colored(
                f'  Duplicated file found: {key}, please download it using web browser.', 'yellow'))
            return
        self.snapshot[key] = value

    def take_snapshot(self):
        """Take a snapshot

        Raises:
            ResourceDoesNotExist: this exception will be raised if file tab is not available

        Returns:
            dict: snapshot of Canvas in `SnapshotFile` or `SnapshotLink` type.
        """
        for _ in self.yield_take_snapshot():
            pass
        return self.get_snapshot()

    def yield_take_snapshot(self):
        course = self.course
        request_batcher = RequestBatcher(course)

        yield (0, '请稍候', '正在获取文件列表')
        files = request_batcher.get_files()
        if files is None:
            raise ResourceDoesNotExist("File tab is not supported.")

        folders = request_batcher.get_folders()

        for _, file in files.items():
            folder = normalize_path(folders[file.folder_id].full_name) + "/"
            if folder.startswith("course files/"):
                folder = folder[len("course files/"):]
            snapshot_file = from_canvas_file(file)
            filename = f'{folder}{normalize_path(snapshot_file.name, file_regex)}'
            self.add_to_snapshot(filename, snapshot_file)

        print(f'  {len(files)} files in total')
        yield (0.1, None, f'共 {len(files)} 个文件')

        if self.with_link:
            yield (None, '正在解析链接', None)
            pages = request_batcher.get_pages() or []
            for page in pages:
                key = f'pages/{normalize_path(page.title, file_regex)}.html'

                # Check if page has body content
                body = getattr(page, 'body', None)
                if body is None or body.strip() == '':
                    # Fallback to redirect link
                    value = SnapshotLink(page.title, page.html_url, "Page")
                else:
                    # Create page with content
                    modified_at = 0
                    if hasattr(page, 'updated_at') and page.updated_at:
                        try:
                            dt = datetime.fromisoformat(page.updated_at.replace('Z', '+00:00'))
                            modified_at = int(dt.timestamp())
                        except:
                            pass

                    value = SnapshotPage(
                        title=page.title,
                        body=body,
                        url=page.html_url,
                        modified_at=modified_at
                    )

                self.add_to_snapshot(key, value)
            print(f'  {len(pages)} pages in total')
            yield (0.2, '请稍候', f'共 {len(pages)} 个链接')

            # Add assignments
            yield (None, '正在获取作业', None)
            assignments = request_batcher.get_assignments() or []
            for assignment in assignments:
                key = f'assignments/{normalize_path(assignment.name, file_regex)}.html'

                # Parse metadata
                modified_at = 0
                if hasattr(assignment, 'updated_at') and assignment.updated_at:
                    try:
                        dt = datetime.fromisoformat(assignment.updated_at.replace('Z', '+00:00'))
                        modified_at = int(dt.timestamp())
                    except:
                        pass

                # Get description
                description = getattr(assignment, 'description', '') or ''

                # Get attachments (if available)
                attachment_files = []
                if hasattr(assignment, 'attachments'):
                    for attach in assignment.attachments:
                        # Convert to SnapshotFile
                        try:
                            snapshot_file = from_canvas_file(attach)
                            attachment_files.append(snapshot_file)

                            # Add attachment to snapshot
                            attach_key = f'assignments/{normalize_path(assignment.name, file_regex)}_files/{normalize_path(snapshot_file.name, file_regex)}'
                            self.add_to_snapshot(attach_key, snapshot_file)
                        except:
                            pass

                # Create SnapshotAssignment
                value = SnapshotAssignment(
                    title=assignment.name,
                    description=description,
                    url=getattr(assignment, 'html_url', ''),
                    modified_at=modified_at,
                    due_at=getattr(assignment, 'due_at', '') or '',
                    points_possible=getattr(assignment, 'points_possible', 0.0) or 0.0,
                    submission_types=getattr(assignment, 'submission_types', []) or [],
                    attachments=attachment_files
                )

                self.add_to_snapshot(key, value)

            print(f'  {len(assignments)} assignments in total')
            yield (0.3, '请稍候', f'共 {len(assignments)} 个作业')

    def get_snapshot(self):
        """Get the previously-taken snapshot

        Returns:
            dict: snapshot of Canvas
        """
        return self.snapshot
