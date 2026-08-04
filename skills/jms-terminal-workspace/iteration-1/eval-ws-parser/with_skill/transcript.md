# Run transcript — eval-ws-parser / with_skill

## Eval Prompt

Implement parse_ws_result(raw, marker) -> (output, rc) with double-marker and
__JMSRC parsing; the sample must return ('hello world', 0); save ws_parse.py
and run the demo.

## Steps

1. Read skills/jms-terminal/SKILL.md core fact 3 and
   references/ws-protocol.md (marker algorithm, __JMSRC capture, double-marker
   semantics).
2. Implemented parse_ws_result (first-marker-line to second-marker output;
   __JMSRC after the second marker).
3. Ran the demo: `python ws_parse.py` → `('hello world', 0)`.

## Result

Sample passes: ('hello world', 0); pure function, no network.
