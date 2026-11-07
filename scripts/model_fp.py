#!/usr/bin/env python3
"""model-fp 命令行入口。

用法：
    python3 model_fp.py probe --base-url https://api.deepseek.com --model deepseek-flash
    python3 model_fp.py compare baselines/a.json baselines/b.json
"""

import sys

from fpkit.cli import main

if __name__ == "__main__":
    sys.exit(main())
