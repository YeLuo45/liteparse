#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
import re

DOCS_DIR = "./docs/src/content/docs/liteparse"
TMP_DIR = f"{DOCS_DIR}/.api-tmp"

path = f"{TMP_DIR}/README.md"
text = open(path).read()
text = re.sub(r"#{2,} Implementations\n.*?(?=\n#{2,} )", "", text, flags=re.DOTALL)
text = re.sub(
    r"#{2,} Trait Implementations\n.*?(?=\n#{2,} )", "", text, flags=re.DOTALL
)
text = text.replace(
    """# Crate Documentation

**Version:** 2.0.0

**Format Version:** 57
""",
    "",
)
open(path, "w").write(text)
