"""Account alias selection following skills/jms-assets (src/jms/core/resources/assets.py).

Priority: @USER > first non-@ alias (fallback to username when alias is
empty) > first account's alias > @INPUT.
"""


def select_account(permed_accounts: list[dict]) -> str:
    """Select the best account alias for a connection token."""
    if not permed_accounts:
        return "@INPUT"
    for acc in permed_accounts:
        if acc.get("alias", "") == "@USER":
            return "@USER"
    for acc in permed_accounts:
        alias = acc.get("alias", "")
        username = acc.get("username", "")
        if alias and not alias.startswith("@"):
            return alias
        if username and not username.startswith("@"):
            return username
    first = permed_accounts[0]
    return first.get("alias", first.get("username", "@INPUT"))


if __name__ == "__main__":
    print(select_account([{"alias": "@USER", "username": "dynamic"},
                          {"alias": "alice", "username": "alice"}]))
    print(select_account([{"alias": "alice", "username": "alice"}]))
    print(select_account([]))
