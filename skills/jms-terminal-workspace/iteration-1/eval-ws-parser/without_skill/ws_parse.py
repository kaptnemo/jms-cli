"""Parse a WebSocket command stream with a done marker."""

import re

RC_RE = re.compile(r"__JMSRC:(\d+)__")


def parse_ws_result(raw: str, marker: str) -> tuple[str, int | None]:
    """Return (text_between_markers, exit_code_or_None)."""
    parts = raw.split(marker)
    if len(parts) < 2:
        return raw.strip(), None
    output = parts[1].strip()
    m = RC_RE.search(raw)
    rc = int(m.group(1)) if m else None
    return output, rc


if __name__ == "__main__":
    raw = "prompt> echo __JMSDONE_x__\nhello world\n__JMSDONE_x__\n__JMSRC:0__\n"
    print(parse_ws_result(raw, "__JMSDONE_x__"))
