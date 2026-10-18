"""House: market making. Asks about spread, adverse selection and volatility."""
from desk.questions.base import OTHER_MEANS, Battery, Choice

BATTERY = Battery(
    bot="house",
    questions=(
        Choice(
            id="h_spread_5m",
            text="Five minutes from now, how will the spread compare with the current spread?",
            options=("wider", "narrower", "same", "other"),
            resolver="spread_change",
            horizon_s=300,
            describe={"wider": "Spread is more than 20% wider than now.",
                      "narrower": "Spread is more than 20% narrower than now.",
                      "same": "Spread is within 20% of the current spread.",
                      "other": OTHER_MEANS},
        ),
        Choice(
            id="h_adverse_1m",
            text="Over the next minute, which resting quote side is more likely to be picked off?",
            options=("bid", "ask", "neither", "other"),
            resolver="adverse_side_1bp",
            horizon_s=60,
            describe={"bid": "Price falls more than 1 bp, so a resting bid would be filled and then lose.",
                      "ask": "Price rises more than 1 bp, so a resting ask would be filled and then lose.",
                      "neither": "Price stays within 1 bp of the current mid.",
                      "other": OTHER_MEANS},
        ),
        Choice(
            id="h_vol_30m",
            text="Compared with the last 30 minutes, how will realized volatility over the next 30 minutes compare?",
            options=("higher", "lower", "similar", "other"),
            resolver="vol_change",
            horizon_s=1800,
            describe={"higher": "Next-30-minute realized volatility is more than 25% above the current `rvol_bps`.",
                      "lower": "Next-30-minute realized volatility is more than 20% below the current `rvol_bps`.",
                      "similar": "Next-30-minute realized volatility is within that band of the current `rvol_bps`.",
                      "other": OTHER_MEANS},
        ),
    ),
)
