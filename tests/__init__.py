import os
import tempfile

from claude_usage import context, paths

# Everything the tool writes goes to a throwaway data folder, never ~/.usage-lens,
# so running the suite never leaves a file behind.
os.environ[paths.DATA_DIR_ENV] = tempfile.mkdtemp(prefix="claude-usage-tests-")
context.MACHINE_ID_FILE = os.path.join(tempfile.mkdtemp(prefix="claude-usage-tests-"), "machine-id")
# ...and never adopts the id an older copy left beside the code.
context.LEGACY_MACHINE_ID_FILE = os.path.join(tempfile.mkdtemp(prefix="claude-usage-tests-"), "machine-id")

# The suite never touches the network. The daily price check honours this, and
# tests that exercise it swap in a stub fetcher and a throwaway cache file.
from claude_usage import price_check  # noqa: E402

os.environ[price_check.OFFLINE_ENV] = "1"
price_check.CACHE_FILE = price_check.Path(tempfile.mkdtemp(prefix="claude-usage-tests-")) / "price-check.json"
