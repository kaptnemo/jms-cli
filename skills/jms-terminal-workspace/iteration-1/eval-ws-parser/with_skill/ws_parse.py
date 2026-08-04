"""WS execute marker parser following skills/jms-terminal/references/ws-protocol.md.

Protocol: the command is sent as ``cmd; __rc=$?; echo <marker>;
echo __JMSRC:${__rc}__``. The marker appears twice in the stream (once
as command echo, once as the echo output). Output is the text after the
first marker's line and before the second marker; the exit code is the
``__JMSRC:N__`` token after the second marker.
"""

import re

RC_RE = re.compile(r"__JMSRC:(\d+)__")


def parse_ws_result(raw: str, marker: str) -> tuple[str, int | None]:
    """Return (command_output, exit_code_or_None) from a WS stream."""
    first = raw.find(marker)
    if first == -1:
        return raw.strip(), None
    after_first = first + len(marker)
    output_start = raw.find("\n", after_first)
    second = raw.find(marker, after_first)
    if second == -1:
        # marker seen once: everything after the first marker is the tail
        tail = raw[after_first:].strip()
        return tail, None
    output = ""
    if output_start != -1 and output_start < second:
        output = raw[output_start:second].strip()
    tail = raw[second + len(marker):]
    m = RC_RE.search(tail)
    rc = int(m.group(1)) if m else None
    return output, rc


if __name__ == "__main__":
    raw = "prompt> echo __JMSDONE_x__\nhello world\n__JMSDONE_x__\n__JMSRC:0__\n"
    print(parse_ws_result(raw, "__JMSDONE_x__"))
