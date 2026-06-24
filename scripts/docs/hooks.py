"""MkDocs lifecycle hooks."""

import shutil
import tempfile
from pathlib import Path

BUILD_TEMP_DIR_CONFIG_KEY = "build_temp_dir"


def on_pre_build(config):
    config[BUILD_TEMP_DIR_CONFIG_KEY] = Path(tempfile.mkdtemp(prefix="ravnar_docs_"))


def on_post_build(config):
    build_temp_dir: Path | None = config.pop(BUILD_TEMP_DIR_CONFIG_KEY, None)
    if build_temp_dir is None or not build_temp_dir.exists():
        return

    shutil.rmtree(build_temp_dir, ignore_errors=True)
