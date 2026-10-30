"""Sync orchestrator skill mirrors from the canonical source; --check never writes.

Run with Python 3.10+ from any directory. No Hub, credentials, or dependencies needed.
"""

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('skills/agent-hub-orchestrator/SKILL.md')
TARGETS = (
    Path('.agents/skills/agent-hub-orchestrator/SKILL.md'),
    Path('.claude/skills/agent-hub-orchestrator/SKILL.md'),
    Path('src/agent_hub/skills/agent-hub-orchestrator/SKILL.md'),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Report missing/stale mirrors without writing')
    args = parser.parse_args()
    try:
        # Universal newlines avoid false drift from Git's Windows CRLF checkout.
        content = (ROOT / SOURCE).read_text(encoding='utf-8')
        stale = []
        for relative in TARGETS:
            target = ROOT / relative
            if target.exists() and target.read_text(encoding='utf-8') == content:
                continue
            stale.append(relative)
            if args.check:
                print(f'Missing or stale: {relative.as_posix()}')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode('utf-8'))
                print(f'Synced: {relative.as_posix()}')
    except (OSError, UnicodeError) as error:
        print(f'Skill sync failed: {error}', file=sys.stderr)
        return 2
    if args.check and stale:
        print('Run: python scripts/sync_skills.py')
        return 1
    print('Skill mirrors are up to date.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
