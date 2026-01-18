class RequestBatcher:
    """RequestBatcher automatically batches requests with batch API
    """

    def __init__(self, course):
        self.course = course
        self.cache = {}

    def get_tabs(self):
        if 'tabs' not in self.cache:
            self.cache['tabs'] = [tab.id for tab in self.course.get_tabs()]

        return self.cache['tabs']

    def get_files(self):
        if 'files' not in self.get_tabs():
            return None

        if 'files' not in self.cache:
            self.cache['files'] = {
                file.id: file
                for file in self.course.get_files()
            }

        return self.cache['files']

    def get_folders(self):
        if 'files' not in self.get_tabs():
            return None

        if 'folders' not in self.cache:
            self.cache['folders'] = {
                folder.id: folder
                for folder in self.course.get_folders()
            }

        return self.cache['folders']

    def get_file(self, file_id):
        files = self.get_files()
        if files is None:
            return self.course.get_file(file_id)
        else:
            return files.get(file_id, self.course.get_file(file_id))

    def get_modules(self):
        if 'modules' not in self.get_tabs():
            return None

        if 'modules' not in self.cache:
            self.cache['modules'] = {
                module.id: module
                for module in self.course.get_modules()
            }

        return self.cache['modules']

    def get_pages(self):
        if 'pages' not in self.get_tabs():
            return None

        if 'pages' not in self.cache:
            # Try to get pages with body in single API call
            pages_list = list(self.course.get_pages(include=['body']))

            # Check if first page has body attribute
            if pages_list and not hasattr(pages_list[0], 'body'):
                # Fallback: fetch each page individually
                print('  Fetching page content individually...')
                self.cache['pages'] = []
                for page_summary in pages_list:
                    try:
                        full_page = self.course.get_page(page_summary.url)
                        self.cache['pages'].append(full_page)
                    except Exception as e:
                        print(f'  Warning: Could not fetch "{page_summary.title}": {e}')
                        self.cache['pages'].append(page_summary)
            else:
                self.cache['pages'] = pages_list

        return self.cache['pages']

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
