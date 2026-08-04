# Run transcript — eval-sftp-token / without_skill

## Eval Prompt

Write create_sftp_token.py using the jms library; state the required
protocol/connect_method values, explain why web_cli cannot be used for SFTP,
and how JMS-{id}/token_value are used on port 2222.

## Steps

1. Did not read the skill; relied on the docstring in
   src/jms/transport/token.py (which states "SFTP must pass
   protocol='sftp', connect_method='web_sftp'").
2. Wrote create_sftp_token.py with protocol="sftp" +
   connect_method="web_sftp".
3. Did not cross-check against live-server tests.

## Result

create_sftp_token.py uses protocol="sftp" — rejected by the real server
(perm_account_invalid); no web_cli explanation or 2222 usage comments.
