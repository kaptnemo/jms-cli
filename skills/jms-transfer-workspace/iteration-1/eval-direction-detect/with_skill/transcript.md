# Run transcript — eval-direction-detect / with_skill

## Eval Prompt

Explain direction detection, give directions and equivalent library calls for
3 invocations, verify example 3 with check_sftp_spec.py, save direction.md.

## Steps

1. Read skills/jms-transfer/SKILL.md sections A/B: colon = remote,
   parse_transfer_spec, sftp_transfer/relay_transfer.
2. Classified 1=upload, 2=download, 3=relay and wrote the
   TransferSpec/RelaySpec equivalents.
3. Ran the bundled `check_sftp_spec.py 'a@s1:/f' 'b@s2:/g'` →
   `relay: a@s1:/f -> b@s2:/g` (exit 0), recorded in direction.md.

## Result

direction.md has the rule, the 3 equivalent calls, and the script's actual
output.
