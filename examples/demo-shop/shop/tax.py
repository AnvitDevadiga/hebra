"""Regional sales tax."""

TAX_RATES = {"us": 0.07, "eu": 0.20, "uk": 0.20}


def tax_for_region(amount, region):
    """Sales tax owed on an amount for a shipping region."""
    rate = TAX_RATES.get(region.lower())
    if rate is None:
        raise KeyError(f"no tax rate for region {region}")
    return amount * rate
