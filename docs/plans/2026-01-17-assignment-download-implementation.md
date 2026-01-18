# Implementation Plan: Assignment Download Feature

## Overview
Add assignment download capability to canvas_grab, allowing users to download assignment descriptions, metadata, and attached files.

## Implementation Steps

### 1. Create SnapshotAssignment Class
**File:** `canvas_grab/snapshot/snapshot_assignment.py` (new)

Create dataclass to store assignment data and generate HTML output:

```python
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
            attachments_html = '<h2>Attachments</h2><ul>'
            for attachment in self.attachments:
                # Relative path to attachment
                rel_path = f'./{escape(self.title)}_files/{escape(attachment.name)}'
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
```

### 2. Update RequestBatcher
**File:** `canvas_grab/request_batcher.py`

Add methods to fetch and filter assignments:

```python
def get_assignments(self):
    if 'assignments' not in self.get_tabs():
        return None

    if 'assignments' not in self.cache:
        all_assignments = list(self.course.get_assignments())

        # Filter out discussions and quizzes
        self.cache['assignments'] = [
            assignment for assignment in all_assignments
            if not self._should_skip_assignment(assignment)
        ]

    return self.cache['assignments']

def _should_skip_assignment(self, assignment):
    """Check if assignment should be skipped based on submission types."""
    submission_types = getattr(assignment, 'submission_types', [])
    return ('discussion_topic' in submission_types or
            'online_quiz' in submission_types)
```

### 3. Update Module Exports
**File:** `canvas_grab/snapshot/__init__.py`

Add export:
```python
from .snapshot_assignment import SnapshotAssignment
```

### 4. Update CanvasFileSnapshot
**File:** `canvas_grab/snapshot/canvas_file_snapshot.py`

Add assignment processing after page processing (around line 107):

```python
# After page processing
if self.with_link:
    # ... existing page code ...

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
```

### 5. Update CanvasModuleSnapshot
**File:** `canvas_grab/snapshot/canvas_module_snapshot.py`

Add same assignment processing as CanvasFileSnapshot after the module processing loop (around line 127):

```python
# After module processing, before unmoduled files
if self.with_link:
    yield (None, '正在获取作业', None)
    assignments = request_batcher.get_assignments() or []

    # Same code as CanvasFileSnapshot for processing assignments
    # ... (identical implementation)

    print(f'  {len(assignments)} assignments in total')
```

### 6. Update Transfer Logic
**File:** `canvas_grab/transfer.py`

Add SnapshotAssignment handling (around line 72):

```python
# Add import
from .snapshot import SnapshotLink, SnapshotFile, SnapshotPage, SnapshotAssignment

# In yield_transfer method after SnapshotPage handling
elif isinstance(plan, SnapshotAssignment):
    Path(path).write_text(plan.content(), encoding='utf-8')
    if plan.modified_at > 0:
        apply_datetime_attr(path, plan.modified_at, plan.modified_at)
```

### 7. Update Planner
**File:** `canvas_grab/planner.py`

Add SnapshotAssignment update detection (around line 46):

```python
# Add import
from .snapshot import SnapshotFile, SnapshotLink, SnapshotPage, SnapshotAssignment

# In plan method after SnapshotPage handling
if isinstance(from_item, SnapshotAssignment):
    content_length = len(from_item.content().encode('utf-8'))
    # Use timestamp if available, otherwise content length
    if from_item.modified_at > 0 and to_item.modified_at != from_item.modified_at:
        plans.append(('update', key, from_item))
    elif to_item.size != content_length:
        plans.append(('update', key, from_item))
```

### 8. Update FileFilter
**File:** `canvas_grab/file_filter.py`

Allow SnapshotAssignment through filters (around line 32):

```python
# Add import
from .snapshot import SnapshotLink, SnapshotPage, SnapshotAssignment

# In filter_files method
return {
    k: v for k, v in snapshot.items()
    if any(map(lambda ext: k.endswith(ext), allowed))
    or isinstance(v, (SnapshotLink, SnapshotPage, SnapshotAssignment))
}
```

## Critical Files to Modify

1. `canvas_grab/snapshot/snapshot_assignment.py` - **CREATE NEW**
2. `canvas_grab/request_batcher.py` - Add get_assignments() and filtering
3. `canvas_grab/snapshot/__init__.py` - Export SnapshotAssignment
4. `canvas_grab/snapshot/canvas_file_snapshot.py` - Process assignments
5. `canvas_grab/snapshot/canvas_module_snapshot.py` - Process assignments
6. `canvas_grab/transfer.py` - Handle SnapshotAssignment transfers
7. `canvas_grab/planner.py` - Update detection for SnapshotAssignment
8. `canvas_grab/file_filter.py` - Allow SnapshotAssignment through filters

## Edge Cases Handled

- **Missing description**: Use empty string, show metadata only
- **Missing timestamps**: Use 0, fall back to content-length comparison
- **Missing metadata**: Use defaults (empty string, 0.0, empty list)
- **No attachments**: Skip attachments section in HTML
- **Attachment download failures**: Try/except, continue processing
- **Discussion/quiz filtering**: Filter based on submission_types array

## Testing Steps

1. **Test with real Canvas course** containing assignments:
   - Run sync with course that has various assignment types
   - Verify `.html` files in `assignments/` folder
   - Check metadata displays correctly
   - Verify attachments download to `_files` subfolders

2. **Test filtering**:
   - Verify discussions not downloaded
   - Verify quizzes not downloaded
   - Verify regular assignments downloaded

3. **Test update detection**:
   - Run sync twice, verify no re-downloads
   - Modify assignment on Canvas
   - Run sync again, verify only modified assignment updates

4. **Test edge cases**:
   - Assignment with no description
   - Assignment with no due date
   - Assignment with no attachments
   - Assignment with multiple attachments

## Backward Compatibility

- Controlled by existing `with_link` option
- No breaking changes to existing functionality
- First sync after implementation will download all assignments
- File and page downloads unaffected
