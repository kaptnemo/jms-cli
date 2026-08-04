# Run transcript — eval-sftp-token / with_skill

## Eval Prompt

Write create_sftp_token.py using the jms library; state the required
protocol/connect_method values, explain why web_cli cannot be used for SFTP,
and how JMS-{id}/token_value are used on port 2222.

## Steps

1. Read skills/jms-auth/SKILL.md core fact 3 and references/endpoints.md:
   SFTP contract = protocol="ssh" + connect_method="web_sftp";
   protocol="sftp" is rejected (perm_account_invalid).
2. Wrote create_sftp_token.py per the skill; comments cover the web_cli/SFTP
   problem and 2222 JMS-{id} auth.
3. Cross-checked against the live-server test conclusion
   (test_connection_token_contract).

## Result

create_sftp_token.py: protocol="ssh" + connect_method="web_sftp"; comments
cover perm_account_invalid and the 2222 usage.
