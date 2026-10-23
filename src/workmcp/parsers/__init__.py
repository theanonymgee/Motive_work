"""Parser dispatch (M1). M0 holds the supported extension set only (design §1.3)."""

SUPPORTED_EXTENSIONS = frozenset({
    '.txt', '.md', '.csv', '.pdf', '.docx', '.pptx', '.xlsx', '.eml', '.hwp', '.hwpx'})
