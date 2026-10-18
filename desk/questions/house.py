"""House: market making. Asks about spread, adverse selection and volatility."""
from desk.questions.base import Battery, Choice

BATTERY = Battery(
    bot="house",
    questions=(
        Choice(
            id="h_spread_5m",
            text="Five minutes from now, how will the spread compare with the current spread?",
            options=("wider", "narrower", "same", "other"),
            resolver="spread_change",
            horizon_s=300,
        ),
        Choice(
            id="h_adverse_1m",
            text="Over the next minute, which resting quote side is more likely to be picked off?",
            options=("bid", "ask", "neither", "other"),
            resolver="adverse_side_1bp",
            horizon_s=60,
        ),
        Choice(
            id="h_vol_30m",
            text="Compared with the last 30 minutes, how will realized volatility over the next 30 minutes compare?",
            options=("higher", "lower", "similar", "other"),
            resolver="vol_change",
            horizon_s=1800,
        ),
    ),
)
