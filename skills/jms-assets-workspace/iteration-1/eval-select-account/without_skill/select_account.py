"""Account alias selection."""


def select_account(permed_accounts: list[dict]) -> str:
    if not permed_accounts:
        return "@INPUT"
    for acc in permed_accounts:
        if acc.get("alias", "").startswith("@"):
            return acc["alias"]
    return permed_accounts[0].get("alias", "@INPUT")
