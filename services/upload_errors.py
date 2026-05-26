"""Upload-related exceptions."""


class DuplicateUploadError(Exception):
    def __init__(self, duplicates: list[dict]):
        self.duplicates = duplicates
        msg = duplicates[0]["message"] if duplicates else "Duplicate upload"
        super().__init__(msg)
