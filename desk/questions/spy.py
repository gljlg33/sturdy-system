"""Spy: reads the order book for informed flow."""
from desk.questions.base import OTHER_MEANS, Battery, Choice

BATTERY = Battery(
    bot="spy",
    questions=(
        Choice(
            id="s_imb_follow_5m",
            text="Over the next 5 minutes, what will price do relative to the current book imbalance direction?",
            options=("follows", "reverses", "flat", "other"),
            resolver="imbalance_follow_2bps",
            horizon_s=300,
            describe={"follows": "Price moves more than 2 bps in the direction the imbalance (`imb`) points.",
                      "reverses": "Price moves more than 2 bps against the direction the imbalance points.",
                      "flat": "Price ends within 2 bps of the current mid.",
                      "other": OTHER_MEANS},
        ),
        Choice(
            id="s_informed_1m",
            text="Is the current top-of-book state more consistent with informed flow than with noise?",
            options=("informed", "noise", "other"),
            # Proxy: |1m return| > 2x spread counts as the book having led price.
            resolver="informed_proxy",
            horizon_s=60,
            describe={"informed": "Within a minute, price moves more than twice the current spread, as if the book knew.",
                      "noise": "Within a minute, price moves less than twice the current spread.",
                      "other": OTHER_MEANS},
        ),
    ),
)
