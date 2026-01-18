# Design: Assignment Download Feature

**Date:** 2026-01-17
**Author:** Michael Saringer with Claude Sonnet 4.5

## Overview

Add capability to download Canvas assignments with their descriptions, metadata, and attached files. Assignments will be stored as HTML files in an `assignments/` folder, similar to how pages are currently handled.

## Goals

- Download assignment descriptions (HTML content)
- Capture assignment metadata (due dates, points possible, submission types)
- Download files attached to assignments
- Filter out discussion topics and online quizzes
- Support timestamp-based sync for efficient updates

## Non-Goals

- Downloading student submissions or grades
- Downloading quiz questions or exam content
- Embedding HTML-referenced images/files locally (keep Canvas URLs)

## Architecture

### Components

Following the existing pattern established by `SnapshotPage`:

1. **SnapshotAssignment** - New dataclass to store assignment data
2. **RequestBatcher.get_assignments()** - Fetches and caches assignments with filtering
3. **CanvasFileSnapshot** - Adds assignments to snapshot in `assignments/` folder
4. **CanvasModuleSnapshot** - Adds assignments to snapshot in `assignments/` folder
5. **Transfer** - Writes assignment HTML files and attachments
6. **Planner** - Detects assignment updates using timestamps
7. **FileFilter** - Allows assignments through regardless of file type filters

### File Organization

```
course_download/
├── assignments/
│   ├── Assignment 1.html
│   ├── Assignment 1_files/
│   │   └── rubric.pdf
│   ├── Assignment 2.html
│   └── Assignment 3.html
├── pages/
│   └── ...
└── files/
    └── ...
```

Both file mode and module mode place assignments in the same `assignments/` folder.

## Data Structure

### SnapshotAssignment Class

```python
@dataclass
class SnapshotAssignment:
    """Canvas assignment with HTML description and metadata."""
    title: str
    description: str  # HTML content from Canvas
    url: str = ''
    modified_at: int = 0  # Unix timestamp from updated_at

    # Metadata
    due_at: str = ''  # ISO 8601 date string
    points_possible: float = 0.0
    submission_types: list = field(default_factory=list)

    # Attachments
    attachments: list = field(default_factory=list)  # List of SnapshotFile

    def content(self):
        """Generate complete HTML document with metadata and description."""
```

### HTML Output Format

Generated HTML includes:
- Document title with assignment name
- Metadata section showing:
  - Due date (formatted)
  - Points possible
  - Submission types
  - Canvas URL
- Assignment description (HTML from Canvas)
- Attachments section (if any) with links to downloaded files

### Attachment Handling

- Attachments are Canvas file objects linked to the assignment
- Each attachment stored as a `SnapshotFile` object
- Downloaded to `assignments/{assignment_name}_files/` subfolder
- HTML links use relative paths: `./{assignment_name}_files/filename.pdf`
- Embedded images/files in HTML description keep Canvas URLs (not downloaded)

## Assignment Filtering

### Inclusion Criteria

Download all assignments **except** those with submission types containing:
- `discussion_topic` - Discussion board posts
- `online_quiz` - Canvas quizzes/exams

### Included Submission Types

- `online_text_entry` - Text submission
- `online_upload` - File upload
- `online_url` - URL submission
- `on_paper` - Physical submission
- `external_tool` - LTI tool integration
- `none` - No submission required (informational assignments)
- `not_graded` - Ungraded assignments

### Implementation

```python
def _should_skip_assignment(self, assignment):
    """Check if assignment should be skipped."""
    submission_types = getattr(assignment, 'submission_types', [])
    return ('discussion_topic' in submission_types or
            'online_quiz' in submission_types)
```

## Canvas API Integration

### Endpoints Used

- `GET /api/v1/courses/:course_id/assignments` via `course.get_assignments()`
- Assignment object includes all needed fields by default

### Fields Retrieved

- `name` - Assignment title
- `description` - HTML content
- `html_url` - Link to Canvas
- `updated_at` - Last modified timestamp
- `due_at` - Due date
- `points_possible` - Point value
- `submission_types` - Array of submission type strings
- `attachments` - Array of attached file objects (if available)

### Error Handling

- If `updated_at` missing, use `created_at` or default to 0
- If description empty, create minimal HTML with metadata only
- If attachments unavailable, skip gracefully
- Print warnings for failed fetches but continue processing

## Update Detection

### Timestamp-Based Sync

Primary method: Compare `modified_at` timestamps
- Canvas `updated_at` → Unix timestamp → `modified_at` field
- Same conversion as pages: `datetime.fromisoformat(updated_at.replace('Z', '+00:00'))`

### Fallback: Content Length

If timestamp unavailable or unchanged:
- Compare byte length of generated HTML content
- Triggers update if content size differs

### Implementation

```python
if isinstance(from_item, SnapshotAssignment):
    content_length = len(from_item.content().encode('utf-8'))
    if from_item.modified_at > 0 and to_item.modified_at != from_item.modified_at:
        plans.append(('update', key, from_item))
    elif to_item.size != content_length:
        plans.append(('update', key, from_item))
```

## Integration Points

### RequestBatcher

Add `get_assignments()` method:
- Check if 'assignments' tab exists
- Cache results in `self.cache['assignments']`
- Filter using `_should_skip_assignment()`
- Return list of assignment objects

### CanvasFileSnapshot

Add to `yield_take_snapshot()` after page processing:
- Fetch assignments via `request_batcher.get_assignments()`
- Create `SnapshotAssignment` objects with timestamp conversion
- Add to snapshot with key: `assignments/{normalized_name}.html`
- Add attachments with key: `assignments/{normalized_name}_files/{filename}`
- Print count: `{len(assignments)} assignments in total`

### CanvasModuleSnapshot

Same integration as `CanvasFileSnapshot`:
- Assignments go to `assignments/` folder regardless of module mode
- Don't process assignment module items (fetch all assignments directly)

### Transfer

Add `SnapshotAssignment` handler:
- Write HTML content to file
- Apply timestamp if available
- Identical to `SnapshotPage` handling

### Planner

Add update detection logic:
- Compare timestamps (preferred)
- Fall back to content length
- Identical to `SnapshotPage` logic

### FileFilter

Add `SnapshotAssignment` to allowed types:
- Assignments bypass file type filters
- Always included like `SnapshotLink` and `SnapshotPage`

## Configuration

### User Options

Assignments controlled by existing `with_link` option:
- When `with_link=True`: Download pages **and** assignments
- When `with_link=False`: Skip both pages and assignments

Rationale: Assignments are similar to pages (HTML content), so grouping makes sense.

Alternative (future): Add separate `with_assignments` option if users want independent control.

## Edge Cases

### Missing Data
- No description: Show metadata only
- No due date: Display "No due date"
- No points: Display "Ungraded" or "0 points"
- No attachments: Omit attachments section

### Special Characters
- Use `normalize_path()` for assignment names
- Handle file name conflicts (existing behavior)

### Large Assignments
- No special handling needed (HTML content typically small)
- Attachments handled by existing file download logic

### Unpublished Assignments
- Canvas API returns only published assignments by default
- No special filtering needed

## Testing Considerations

### Manual Testing
1. Course with various assignment types
2. Verify quiz/discussion filtering works
3. Check attachment downloads
4. Verify timestamp-based updates
5. Test with missing metadata fields

### Validation
- Assignment count matches Canvas (minus filtered types)
- HTML renders correctly in browser
- Attachments download and links work
- Re-sync doesn't re-download unchanged assignments

## Backward Compatibility

- New feature, no breaking changes
- Controlled by existing `with_link` option
- No impact on file or page downloads
- First sync will download all assignments

## Future Enhancements

- Separate `with_assignments` configuration option
- Download embedded HTML images/files
- Support for assignment groups/categories
- Include submission status/grades (requires different scope)

## References

- Canvas Assignments API: https://developerdocs.instructure.com/services/canvas/resources/assignments
- canvasapi library: https://canvasapi.readthedocs.io/en/stable/course-ref.html
- Existing SnapshotPage implementation
