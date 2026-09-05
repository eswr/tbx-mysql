"""Sensitive data masking before results leave the engine."""


def mask_account_number(account_number: str | None) -> str | None:
    """Mask account number, keeping last 4 digits."""
    if not account_number or len(account_number) <= 4:
        return account_number
    return f"XXXXX{account_number[-4:]}"


def mask_utr(utr: str | None) -> str | None:
    """Mask UTR, keeping first 4 and last 2 characters."""
    if not utr:
        return None
    if len(utr) <= 6:
        return utr[:2] + "***"
    return utr[:4] + "***" + utr[-2:]


def mask_record(record: dict) -> dict:
    """Apply masking to a database record."""
    masked = dict(record)

    for key in ["account_number", "account_number_masked"]:
        if key in masked and masked[key]:
            masked[key] = mask_account_number(masked[key])

    if "utr_number" in masked and masked["utr_number"]:
        masked["utr_number"] = mask_utr(masked["utr_number"])

    return masked
