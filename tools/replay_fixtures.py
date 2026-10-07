"""Classify captured ScreenFrame JSONL without connecting to any terminal."""
import argparse
import json
from pathlib import Path

from cliretry.models import ScreenFrame, as_json
from cliretry.profiles import CodexProfile


def replay(path):
    profile = CodexProfile()
    with Path(path).open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            f = ScreenFrame.from_dict(json.loads(line))
            print(json.dumps({"frame": index, "observation": as_json(profile.classify(f))}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    replay(parser.parse_args().path)

