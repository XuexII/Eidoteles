import sys
from typing import Dict

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def load_yaml(path, encoding="utf-8") -> Dict:
    with open(path, "r", encoding=encoding) as f:
        return yaml.safe_load(f)


def load_toml(path) -> Dict:
    with open(path, "rb") as f:
        return tomllib.load(f)
